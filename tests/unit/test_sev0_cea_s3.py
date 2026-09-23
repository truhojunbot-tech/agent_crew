"""SEV-0 CEA step 3 — input providers, the P2a executor-binding broker, memory-backed reuse.

Contract: alfred ``sev0/e11-adr-draft`` @ ``6cbce565`` (Π P1, P2a, P6, P7, §5, §6, O9);
receipt schema alfred ``sev0/cea-alfred-lineage`` @ ``e1063eb`` (every receipt below
is validated against it by the engine's own ``_record``).
"""
from __future__ import annotations

import ast
import json
import os
import stat
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_crew.cea import store as receipt_store
from agent_crew.cea.auth import AdapterIdentity, StaticTokenAuthenticator
from agent_crew.cea.broker import (
    BLOCKED, BROKER_SPAWNED, Broker, BrokerClient, BrokerRefused, BrokerSpawnRequest,
    DISPATCHER_REGISTRATION_UNAUTHENTICATED, PEER_ASSERTED, ProcReader, UNVERIFIED, VERIFIED,
    start_time_of)
from agent_crew.cea.engine import AuthorizationEngine, EngineConfig
from agent_crew.cea.input_providers import (
    AdmissionInputsClient, CanonicalPolicySnapshotReader, E4CapabilityProvider, InputUnavailable,
    QueueRuntimeStateProvider, QuotaBudgetProvider, SnapshotRecordGate)
from agent_crew.cea.intent import CallerProvenance, Intent, IntentIdentity, Target, WorkClass
from agent_crew.cea.memory import MemoryGate, memory_providers
from agent_crew.cea.providers import PolicySnapshotRef, SignatureStatus
from agent_crew.cea.receipt import BudgetClass, DecisionRev, HumanGate, HumanGateState, ProviderBudget
from agent_crew.cea.runtime_state import RuntimeState, RuntimeStateSnapshot
from agent_crew.cea.schema import validate_receipt

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
FIXTURE = REPO / "tests" / "fixtures" / "cea_memory" / "known_duplicate.admission_inputs.json"
LAUNCHER = REPO / "tools" / "cea" / "broker-launch.sh"
DECISION = DecisionRev(decision_id="T0-1234", body_hash="b" * 32)


# ── helpers ─────────────────────────────────────────────────────────────────

def caller(principal, provenance):
    tok = f"t::{principal}"
    return StaticTokenAuthenticator({tok: AdapterIdentity(principal, provenance)}).authenticate(tok)


def intent(task_id="t1", *, project="alfred", capability_id="quota.claude-usage-monitor",
           description="add a transcript usage meter", anchors=("quota_cache.json",)):
    return Intent(identity=IntentIdentity(project=project, work_class=WorkClass.IMPLEMENT,
                                          target=Target(repo="truhojunbot-tech/alfred", base_ref="main",
                                                        scope_anchors=tuple(anchors)),
                                          capability_id=capability_id,
                                          authority_decision_ids=("T0-1234",)),
                  task_id=task_id, task_type="implement", description=description)


class Snap:
    def __init__(self, **kw):
        self.kw = kw

    def current(self, intent=None):
        base = dict(generation=7, hash="h" * 16, produced_at=None, decisions=(DECISION,),
                    in_scope=(DECISION,), signature=SignatureStatus.VALID, available=True, tier="T0")
        base.update(self.kw)
        return PolicySnapshotRef(**base)


class Runtime:
    def current(self):
        return RuntimeStateSnapshot(state=RuntimeState.ACTIVE, epoch=3)


class Budget:
    def budget(self, provider):
        return ProviderBudget(provider=provider, state=BudgetClass.OK, observed_at=None)


def fixture_client(doc=None):
    body = json.loads(FIXTURE.read_text()) if doc is None else doc
    return AdmissionInputsClient(runner=lambda req: json.loads(json.dumps(body)))


def engine_with(client, **kw):
    prov = memory_providers(E4CapabilityProvider(client), SnapshotRecordGate(), client)
    prov.update(kw)
    return AuthorizationEngine(config=EngineConfig(mode="enforce"), snapshots=kw.pop("snapshots", Snap()),
                               runtime=Runtime(), budgets=Budget(),
                               **{k: v for k, v in prov.items() if k not in ("snapshots",)})


# ⛔`_authorize_authenticated`, not `authorize`. These are `mode=enforce`
#   decision tests, and under enforce the *public* `authorize()` is
#   unconditionally fail-closed — it is the entry point any in-process caller
#   can reach, so it can never be the one that enforces (codex
#   review-sev0-cea-lineage-s2a-fix-r3-x P1). In the real deployment only
#   `service.EngineService` reaches this method, after the presented credential
#   matched by `hmac.compare_digest`, over a socket, in another process. Here
#   the harness stands in for that boundary so the *judgements* below can be
#   exercised; that the boundary itself cannot be faked is asserted in
#   tests/unit/test_sev0_cea_s2a_fix_r3.py, not here.


def mem_conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    receipt_store.ensure_schema(c)
    return c


# ── (a) providers: UNAVAILABLE ⇒ fail closed ────────────────────────────────

def _raise(_req):
    raise InputUnavailable("alfred provider path missing")


def test_e4_provider_unavailable_blocks():
    client = AdmissionInputsClient(runner=_raise)
    auth = engine_with(client)._authorize_authenticated(mem_conn(), intent(), caller("cron:a", CallerProvenance.CRON))
    assert auth.decision == "BLOCK" and auth.code == "INPUTS_UNAVAILABLE"
    assert "capability_registry" in auth.receipt["reason"]["text"]
    assert not validate_receipt(auth.receipt)


def test_missing_provider_script_is_unavailable_not_no_matches(tmp_path):
    client = AdmissionInputsClient(str(tmp_path / "nope.py"))
    assert E4CapabilityProvider(client).lookup(intent()).available is False
    with pytest.raises(InputUnavailable):
        client.provide(intent())


def test_registry_unavailable_status_and_degraded_stale():
    doc = json.loads(FIXTURE.read_text())
    doc["capability"]["status"] = "UNAVAILABLE"
    assert E4CapabilityProvider(fixture_client(doc)).lookup(intent()).available is False
    doc = json.loads(FIXTURE.read_text())
    doc["capability"]["status"] = "DEGRADED"
    assert E4CapabilityProvider(fixture_client(doc)).lookup(intent()).stale is True


def test_l3_memory_unavailable_blocks_even_when_e4_answers():
    doc = json.loads(FIXTURE.read_text())
    doc["incident_memory"] = {"provider": "incident_memory", "status": "UNAVAILABLE", "matches": []}
    auth = engine_with(fixture_client(doc))._authorize_authenticated(mem_conn(), intent(),
                                                      caller("cron:a", CallerProvenance.CRON))
    assert auth.decision == "BLOCK" and auth.code == "INPUTS_UNAVAILABLE"


def test_snapshot_reader_missing_unkeyed_stale(tmp_path):
    missing = CanonicalPolicySnapshotReader(str(tmp_path / "none.json")).current()
    assert missing.available is False
    p = tmp_path / "snap.json"
    now = time.time()
    p.write_text(json.dumps({"generation": 3, "produced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                             "decisions": [{"decision_id": "T0-1", "body_hash": "x" * 32,
                                            "scope": {"project": "alfred"}}],
                             "signature": {"alg": "ed25519", "key_id": "k", "value": "v"}}))
    snap = CanonicalPolicySnapshotReader(str(p), clock=lambda: now).current(intent())
    assert snap.available and snap.signature is SignatureStatus.UNKEYED   # no verifier ⇒ unverified
    assert [r.decision_id for r in snap.in_scope] == ["T0-1"] and snap.hash.startswith("sha256:")
    assert CanonicalPolicySnapshotReader(str(p), clock=lambda: now + 10 ** 6).current().available is False
    ok = CanonicalPolicySnapshotReader(str(p), clock=lambda: now,
                                       verifier=lambda body, sig: SignatureStatus.VALID).current()
    assert ok.signature is SignatureStatus.VALID
    # engine: an unkeyed snapshot is an UNVERIFIED input ⇒ BLOCK
    eng = AuthorizationEngine(config=EngineConfig(mode="enforce"),
                              snapshots=CanonicalPolicySnapshotReader(str(p), clock=time.time),
                              capabilities=E4CapabilityProvider(fixture_client()),
                              runtime=Runtime(), budgets=Budget())
    auth = eng._authorize_authenticated(mem_conn(), intent(), caller("cron:a", CallerProvenance.CRON))
    assert auth.decision == "BLOCK" and "policy_snapshot_signature:UNKEYED" in auth.receipt["reason"]["text"]


def test_runtime_provider_reads_row_readonly_and_fails_closed(tmp_path):
    from agent_crew.queue import TaskQueue
    db = tmp_path / "q.db"
    TaskQueue(str(db))
    snap = QueueRuntimeStateProvider(str(db)).current()
    assert snap.read_failed is False and snap.state is RuntimeState.ACTIVE
    gone = QueueRuntimeStateProvider(str(tmp_path / "missing.db")).current()
    assert gone.read_failed and gone.state is RuntimeState.STOPPED


def test_budget_o9_stale_paid_closed_plan_open_with_receipt(tmp_path):
    d = tmp_path / "claude_monitor"
    d.mkdir()
    now = 2_000_000_000.0
    (d / "quota_cache.json").write_text(json.dumps({"fetched_at": now - 10 ** 5, "error": None,
                                                    "five_hour": {"utilization": 0.1}}))
    paid = QuotaBudgetProvider(str(tmp_path), credit_class={"claude": "paid"}, clock=lambda: now)
    plan = QuotaBudgetProvider(str(tmp_path), credit_class={"claude": "plan"}, clock=lambda: now)
    assert paid.budget("claude").state is BudgetClass.EXHAUSTED
    b = plan.budget("claude")
    assert b.state is BudgetClass.CONSTRAINED and b.observed_at == now - 10 ** 5
    assert paid.budget("codex").state is BudgetClass.EXHAUSTED          # no cache at all
    (d / "quota_cache.json").write_text(json.dumps({"fetched_at": now, "five_hour": {"utilization": 0.95}}))
    assert plan.budget("claude").state is BudgetClass.CONSTRAINED
    cd = tmp_path / "cooldown.json"
    cd.write_text(json.dumps({"claude": now + 60}))
    assert QuotaBudgetProvider(str(tmp_path), cooldown_file=str(cd), clock=lambda: now,
                               credit_class={"claude": "plan"}).budget("claude").state is BudgetClass.EXHAUSTED


def test_j8_grant_needs_a_record_in_the_snapshot():
    snap = Snap(human_gate_predicates=({"project": "alfred", "state": "GRANTED", "decision_id": "T0-9999"},)).current()
    assert SnapshotRecordGate().state(intent(), snap).state is HumanGateState.PENDING
    snap = Snap(human_gate_predicates=({"project": "alfred", "state": "GRANTED", "decision_id": "T0-1234"},)).current()
    assert SnapshotRecordGate().state(intent(), snap).state is HumanGateState.GRANTED


# ── (c) memory-backed reuse proof ───────────────────────────────────────────

ADAPTERS = [("cron:admitted_trigger", CallerProvenance.CRON), ("direct:curl", CallerProvenance.DIRECT)]


def _queued_rows(db) -> int:
    c = sqlite3.connect(db)
    try:
        return c.execute("SELECT COUNT(*) FROM tasks WHERE status = 'pending'").fetchone()[0]
    finally:
        c.close()


def _enforcing_adapter(eng, db, it, who):
    """An enforce-mode §7 adapter: a task row exists only for an ALLOW/REVIEW admission."""
    from agent_crew.queue import TaskQueue
    from agent_crew.protocol import TaskRequest
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        auth = eng._authorize_authenticated(conn, it, caller(*who))
        conn.commit()
    finally:
        conn.close()
    if auth.decision in ("ALLOW", "REVIEW"):
        TaskQueue(db).enqueue(TaskRequest(task_id=it.task_id, task_type="implement",
                                          description=it.description))
    return auth


def test_known_duplicate_same_decision_through_two_adapters_and_no_new_task(tmp_path):
    from agent_crew.queue import TaskQueue
    outcomes = []
    for i, who in enumerate(ADAPTERS):
        db = str(tmp_path / f"q{i}.db")
        TaskQueue(db)
        before = _queued_rows(db)
        auth = _enforcing_adapter(engine_with(fixture_client()), db, intent(task_id=f"t-{i}"), who)
        assert _queued_rows(db) == before == 0
        assert not validate_receipt(auth.receipt)
        outcomes.append((auth.decision, auth.code, auth.receipt["reuse"],
                         auth.receipt["matched_capability"], auth.receipt["human_gate_state"]))
    assert outcomes[0] == outcomes[1]
    decision, code, reuse, matched, gate = outcomes[0]
    assert decision == "HUMAN_GATE" and code == "HUMAN_GATE_PENDING"
    assert reuse["decision"] == "REUSE" and matched["id"] == "quota.claude-usage-monitor"


def test_known_duplicate_one_db_second_adapter_refused_no_task(tmp_path):
    from agent_crew.queue import TaskQueue
    db = str(tmp_path / "q.db")
    TaskQueue(db)
    eng = engine_with(fixture_client())
    a = _enforcing_adapter(eng, db, intent(task_id="first"), ADAPTERS[0])
    b = _enforcing_adapter(eng, db, intent(task_id="second", description="same meter, reworded"), ADAPTERS[1])
    assert a.decision == "HUMAN_GATE" and b.decision == "BLOCK" and b.code == "DUPLICATE_INTENT"
    assert _queued_rows(db) == 0


def test_memory_block_disposition_denies_and_snapshot_supersession_lifts():
    doc = json.loads(FIXTURE.read_text())
    doc["incident_memory"]["matches"][0].update(recorded_disposition="BLOCK", kind="counterexample")
    auth = engine_with(fixture_client(doc))._authorize_authenticated(mem_conn(), intent(), caller(*ADAPTERS[0]))
    assert auth.decision == "BLOCK" and auth.code == "HUMAN_GATE_DENIED"
    lifted = DecisionRev(decision_id="T0-1234", body_hash="b" * 32, supersedes=("DW-ALFRED-USAGE-METER",))
    gate = MemoryGate(SnapshotRecordGate(), fixture_client(doc)).state(
        intent(), Snap(decisions=(lifted,), in_scope=(lifted,)).current())
    assert gate.state is HumanGateState.NOT_REQUIRED


def test_low_confidence_memory_never_tightens():
    doc = json.loads(FIXTURE.read_text())
    doc["incident_memory"]["matches"][0]["match"]["confidence"] = "LOW_CONFIDENCE"
    gate = MemoryGate(SnapshotRecordGate(), fixture_client(doc)).state(intent(), Snap().current())
    assert gate.state is HumanGateState.NOT_REQUIRED


def test_memory_fixture_is_permanent_and_contract_shaped():
    doc = json.loads(FIXTURE.read_text())
    assert doc["schema"] == "admission-inputs/v1" and "_provenance" in doc
    entry = doc["incident_memory"]["matches"][0]
    assert set(entry) == {"id", "kind", "recorded_disposition", "title", "invariant", "match"}


# ── (b) broker ──────────────────────────────────────────────────────────────

class FakeProc(ProcReader):
    def __init__(self, table):
        self.table = table            # pid -> (ppid, start_time)

    def stat(self, pid):
        return self.table.get(pid)

    def tracer(self, pid):
        return 0


def scope_file(tmp_path, value="1"):
    p = tmp_path / "ptrace_scope"
    p.write_text(value)
    return str(p)


def test_broker_refuses_ptrace_scope_zero_and_foreign_euid(tmp_path):
    with pytest.raises(BrokerRefused):
        Broker(str(tmp_path), degraded=True, ptrace_scope_path=scope_file(tmp_path, "0")).preflight()
    with pytest.raises(BrokerRefused):
        Broker(str(tmp_path), service_uid_override=os.geteuid() + 1,
               ptrace_scope_path=scope_file(tmp_path)).preflight()
    assert Broker(str(tmp_path), degraded=True, ptrace_scope_path=scope_file(tmp_path)).preflight()["degraded"]


def test_broker_refuses_descendant_of_another_agent_and_foreign_children():
    me = 100
    proc = FakeProc({200: (me, 5), 300: (200, 7), 400: (999, 9)})
    b = Broker("/nonexistent", proc=proc, client_uids=(1000,))
    assert b.handle(me, 1000, {"op": "register", "pid": 200, "start_time": 5, "receipt_id": "r", "attempt": 1})["ok"]
    # 300 is a child of registered executor 200: inside another agent's tree
    assert b.handle(200, 1000, {"op": "register", "pid": 300, "start_time": 7, "receipt_id": "r2",
                                "attempt": 1})["error"] == "DESCENDANT_OF_ANOTHER_AGENT"
    assert b.handle(me, 1000, {"op": "register", "pid": 400, "start_time": 9, "receipt_id": "r3",
                               "attempt": 1})["error"] == "NOT_A_CHILD_OF_REGISTRANT"
    assert b.handle(me, 1000, {"op": "register", "pid": 200, "start_time": 6, "receipt_id": "r4",
                               "attempt": 1})["error"] == "PID_START_TIME_MISMATCH"


def test_broker_second_registration_poisons_the_pair():
    proc = FakeProc({200: (100, 5), 201: (100, 6)})
    b = Broker("/x", proc=proc)
    assert b.handle(100, 1000, {"op": "register", "pid": 200, "start_time": 5, "receipt_id": "r", "attempt": 1})["ok"]
    assert b.handle(100, 1000, {"op": "register", "pid": 201, "start_time": 6, "receipt_id": "r",
                                "attempt": 1})["error"] == "REGISTRATION_CONFLICT"
    assert b.handle(200, 1000, {"op": "attest", "receipt_id": "r", "attempt": 1})["executor_binding_status"] == BLOCKED


def test_degraded_broker_never_issues_verified():
    b = Broker("/x", proc=FakeProc({200: (100, 5)}), degraded=True)
    b.handle(100, 1000, {"op": "register", "pid": 200, "start_time": 5, "receipt_id": "r", "attempt": 1})
    out = b.handle(200, 1000, {"op": "attest", "receipt_id": "r", "attempt": 1})
    assert out["executor_binding_status"] == UNVERIFIED and out["caller_identity_status"] == UNVERIFIED


def test_same_uid_impersonation_without_broker_is_unverified(tmp_path):
    out = BrokerClient(str(tmp_path / "no.sock"), expected_uid=0).attest("r", 1)
    assert out["executor_binding_status"] == UNVERIFIED and out["caller_identity_status"] == UNVERIFIED


_ATTEST = """
import json, sys
sys.path.insert(0, {src!r})
from agent_crew.cea.broker import BrokerClient
sys.stdin.readline()
print(json.dumps(BrokerClient({sock!r}, expected_uid={uid}).attest({rcpt!r}, 1)), flush=True)
"""


@pytest.fixture()
def live_broker(tmp_path):
    d = tmp_path / "sock"
    d.mkdir(mode=0o710)   # bind() accepts 0710 to the client group and nothing else
    b = Broker(str(d), service_uid_override=os.geteuid(), ptrace_scope_path=scope_file(tmp_path),
               client_uids=(os.geteuid(),))
    b.preflight()
    b.bind()
    t = threading.Thread(target=b.serve_forever, daemon=True)
    t.start()
    yield b
    b.close()


def _spawn_attester(sock, rcpt="rcpt-1"):
    code = _ATTEST.format(src=str(SRC), sock=sock, uid=os.geteuid(), rcpt=rcpt)
    return subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True)


def _attest_via(p):
    out, _ = p.communicate("go\n", timeout=20)
    return json.loads(out.strip().splitlines()[-1])


def test_broker_peer_cred_binding_real_socket(live_broker):
    """The binding still discriminates — a same-uid process of a different pid
    naming the same (receipt_id, attempt) is BLOCKED by SO_PEERCRED, not by
    anything it sent — but the process that *does* match is still only
    UNVERIFIED, because matching proves the registrant's own child, not the
    dispatcher's (codex review-sev0-cea-lineage-s3-x P1)."""
    sock = live_broker.sock_path
    executor = _spawn_attester(sock)
    impostor = _spawn_attester(sock)
    try:
        st = start_time_of(executor.pid)
        reg = BrokerClient(sock, expected_uid=os.geteuid()).register(executor.pid, st, "rcpt-1", 1)
        assert reg["ok"] is True
        assert reg["registration_authentication"] == DISPATCHER_REGISTRATION_UNAUTHENTICATED
        bad = _attest_via(impostor)
        good = _attest_via(executor)
    finally:
        for p in (executor, impostor):
            if p.poll() is None:
                p.kill()
    assert bad["executor_binding_status"] == BLOCKED and bad["reason"] == "PEER_CRED_MISMATCH"
    assert good["executor_binding_status"] == UNVERIFIED
    assert good["downgrade_reason"] == DISPATCHER_REGISTRATION_UNAUTHENTICATED
    assert good["binding_evidence"]["pid"] == executor.pid and good["nonce"] is None
    assert good["caller_identity_status"] == UNVERIFIED and bad["caller_identity_status"] == UNVERIFIED


# ── the exact self-registration attack from codex P1 ────────────────────────

def test_self_registration_by_a_same_uid_attacker_is_never_verified(live_broker):
    """codex review-sev0-cea-lineage-s3-x P1, verbatim reproduction.

    "An arbitrary same-UID process can fork child PID 201 and call
    register(pid=201, start_time=11, receipt_id='victim-receipt', attempt=1),
    then that child attests and receives executor_binding_status=VERIFIED."

    Nothing here is stopped: the fork succeeds, the registration succeeds
    (``ok: true``), the child is genuinely the attacker's direct child with a
    genuine start_time, SO_PEERCRED is genuine, and the attest matches. Every
    one of those checks passes and always will, because they are all questions
    about the *attacker's own* process tree. What must never happen is the
    answer VERIFIED, and that is what is asserted.
    """
    sock = live_broker.sock_path
    attacker_child = _spawn_attester(sock, "victim-receipt")   # "child PID 201"
    try:
        st = start_time_of(attacker_child.pid)      # "start_time=11"
        reg = BrokerClient(sock, expected_uid=os.geteuid()).register(
            attacker_child.pid, st, "victim-receipt", 1)
        assert reg["ok"] is True, "the attack is not prevented; only its reward is removed"
        out = _attest_via(attacker_child)
    finally:
        if attacker_child.poll() is None:
            attacker_child.kill()

    assert out["executor_binding_status"] != VERIFIED
    assert out["executor_binding_status"] == UNVERIFIED
    assert out["downgrade_reason"] == DISPATCHER_REGISTRATION_UNAUTHENTICATED
    assert out["caller_identity_status"] == UNVERIFIED
    assert out["nonce"] is None
    # the tuple survives as evidence — the point is that it is labelled as such.
    assert out["binding_evidence"]["origin"] == PEER_ASSERTED


def test_no_registration_this_module_can_create_is_broker_spawned():
    """The only origin that could reach VERIFIED is one no code path produces."""
    b = Broker("/x", proc=FakeProc({200: (100, 5)}))
    b.handle(100, 1000, {"op": "register", "pid": 200, "start_time": 5,
                         "receipt_id": "r", "attempt": 1})
    assert [r.origin for r in b._regs.values()] == [PEER_ASSERTED]
    assert BROKER_SPAWNED not in {r.origin for r in b._regs.values()}


def test_broker_spawn_is_the_deferred_interface_and_answers_unavailable():
    """O21c: the interface exists so a caller can ask and be refused, instead of
    silently falling back to the peer-asserted path."""
    b = Broker("/x", proc=FakeProc({}))
    out = b.spawn(BrokerSpawnRequest(receipt_id="r", attempt=1, argv=("/bin/true",)))
    assert out["ok"] is False and out["status"] == "UNAVAILABLE" and out["deferred"] == "O21c"
    assert "broker_performs_the_fork_exec" in out["requires"]
    assert "dispatcher_runs_as_a_different_uid" in out["requires"]
    # and over the wire, same answer, still no registration created.
    wire = b.handle(100, 1000, {"op": "spawn", "receipt_id": "r", "attempt": 1, "argv": []})
    assert wire["status"] == "UNAVAILABLE" and b._regs == {}


def test_socket_dir_0711_is_refused_and_the_socket_is_never_0666(tmp_path):
    """The 0666 fallback is gone: 0711 dirs no longer bind at all."""
    wide = tmp_path / "wide"
    wide.mkdir(mode=0o711)
    b = Broker(str(wide), service_uid_override=os.geteuid(), client_uids=(os.geteuid(),))
    with pytest.raises(BrokerRefused) as exc:
        b.bind()
    assert "0710" in str(exc.value)

    tight = tmp_path / "tight"
    tight.mkdir(mode=0o710)
    ok = Broker(str(tight), service_uid_override=os.geteuid(), client_uids=(os.geteuid(),))
    ok.bind()
    try:
        assert stat.S_IMODE(os.stat(ok.sock_path).st_mode) == 0o660
    finally:
        ok.close()


def test_socket_dir_group_must_be_a_group_the_clients_are_in(tmp_path):
    """0710 to a group no client is in is not a boundary, it is an outage
    dressed as one — and it would be a silent one."""
    d = tmp_path / "d"
    d.mkdir(mode=0o710)
    # uid 0's primary group is root(0); the test uid is not in it.
    b = Broker(str(d), service_uid_override=os.geteuid(), client_uids=(0,))
    if os.stat(d).st_gid == 0 or os.geteuid() == 0:
        pytest.skip("test uid's group is root; the negative case is not expressible here")
    with pytest.raises(BrokerRefused) as exc:
        b.bind()
    assert "not a group of any client uid" in str(exc.value)


def test_client_downgrades_verified_from_a_non_service_peer(live_broker):
    # The live broker here runs in the test uid; a client expecting crew-authz
    # must not believe its VERIFIED.
    p = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.readline()"], stdin=subprocess.PIPE, text=True)
    try:
        live_broker._regs.clear()
        out = BrokerClient(live_broker.sock_path, expected_uid=os.geteuid() + 12345).attest("nobody", 1)
        assert out["executor_binding_status"] == UNVERIFIED
    finally:
        p.kill()


# ── static: nothing but the broker promotes to VERIFIED ─────────────────────

STATUS_FIELDS = {"executor_binding_status", "caller_identity_status"}


def _mentions_verified(node) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and n.value == "VERIFIED":
            return True
        if isinstance(n, ast.Attribute) and n.attr == "VERIFIED":
            return True
        if isinstance(n, ast.Name) and n.id == "VERIFIED":
            return True
    return False


# The one function allowed to produce executor_binding_status=VERIFIED. It is
# O21c and unreachable today (only a BROKER_SPAWNED registration reaches it, and
# `spawn()` returns UNAVAILABLE) — but when O21c lands, this is where it lands,
# and anything else that starts saying VERIFIED fails this test instead of
# shipping. codex P1 asked for broker.py to be covered here too; file-level
# granularity was what let the peer-asserted path say VERIFIED unnoticed.
DEFERRED_VERIFIED_PATH = ("agent_crew/cea/broker.py", "_attest_broker_spawned")


def _enclosing_functions(tree):
    """{lineno: qualified function name} for every line inside a function body."""
    spans = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            for line in range(node.lineno, end + 1):
                # innermost wins: nested defs are walked after their parent only
                # by accident, so prefer the tightest span seen.
                prev = spans.get(line)
                if prev is None or (end - node.lineno) < prev[1]:
                    spans[line] = (node.name, end - node.lineno)
    return {line: name for line, (name, _) in spans.items()}


def test_no_verified_promotion_path_outside_the_broker():
    offenders = []
    for path in (SRC / "agent_crew").rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        fns = _enclosing_functions(tree)
        for node in ast.walk(tree):
            pairs = []
            if isinstance(node, ast.Dict):
                pairs = [(k.value, v) for k, v in zip(node.keys, node.values)
                         if isinstance(k, ast.Constant) and k.value in STATUS_FIELDS]
            elif isinstance(node, ast.keyword) and node.arg in STATUS_FIELDS:
                pairs = [(node.arg, node.value)]
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) \
                            and t.slice.value in STATUS_FIELDS and node.value is not None:
                        pairs.append((t.slice.value, node.value))
                    if isinstance(t, ast.Attribute) and t.attr in STATUS_FIELDS and node.value is not None:
                        pairs.append((t.attr, node.value))
            for field, value in pairs:
                if not _mentions_verified(value):
                    continue
                line = node.lineno if hasattr(node, "lineno") else value.lineno
                allowed = (rel, fns.get(line)) == DEFERRED_VERIFIED_PATH \
                    and field == "executor_binding_status"
                if not allowed:
                    offenders.append(f"{rel}:{line} {field} in {fns.get(line)!r}")
    assert offenders == [], offenders


def test_the_reachable_broker_attest_path_does_not_mention_verified():
    """Complement to the static sweep: the *live* decision function. If
    `_attest` ever grows a VERIFIED branch again, this fails whether or not it
    is spelled as a status-field assignment."""
    import inspect
    import textwrap

    from agent_crew.cea import broker as broker_mod

    src = textwrap.dedent(inspect.getsource(broker_mod.Broker._attest))
    tree = ast.parse(src)
    calls = [n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    assert "_attest_broker_spawned" in calls, "the deferred path must still be dispatched to"
    stripped = ast.parse(src)
    for node in ast.walk(stripped):
        if isinstance(node, ast.Return) and node.value is not None:
            src = ast.dump(node.value)
            assert "'VERIFIED'" not in src and "VERIFIED" not in _names(node.value), \
                ast.dump(node)


def _names(node) -> set:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | \
           {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}


def test_broker_never_writes_caller_identity_verified():
    tree = ast.parse((SRC / "agent_crew" / "cea" / "broker.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "caller_identity_status":
            assert not _mentions_verified(node.value)
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "caller_identity_status":
                    assert not _mentions_verified(v)


# ── launcher (no sudo; fake euid by env only) ───────────────────────────────

def _launch(tmp_path, *args, **env):
    e = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
         "AGENT_CREW_AUTHZ_SOCK_DIR": str(tmp_path / "authz"),
         "AGENT_CREW_AUTHZ_PTRACE_SCOPE_FILE": scope_file(tmp_path, env.pop("scope", "1")),
         "AGENT_CREW_AUTHZ_PYTHONPATH": str(SRC), "AGENT_CREW_AUTHZ_DRY_RUN": "1"}
    e.update(env)
    return subprocess.run(["bash", str(LAUNCHER), *args], env=e, capture_output=True, text=True, timeout=20)


def test_launcher_refuses_wrong_euid_and_low_ptrace_scope(tmp_path):
    r = _launch(tmp_path)
    assert r.returncode == 3 and "not crew-authz" in r.stderr
    r = _launch(tmp_path, scope="0", AGENT_CREW_AUTHZ_FAKE_EUID="998")
    assert r.returncode == 3 and "ptrace_scope" in r.stderr


def test_launcher_fake_euid_runs_and_degraded_passes_flag(tmp_path):
    r = _launch(tmp_path, AGENT_CREW_AUTHZ_FAKE_EUID=str(_crew_authz_uid()))
    assert r.returncode == 0 and "--degraded" not in r.stdout, r.stderr
    mode = os.stat(tmp_path / "authz").st_mode & 0o777
    assert mode in (0o710, 0o711)
    r = _launch(tmp_path, "--degraded")
    assert r.returncode == 0 and "--degraded" in r.stdout


def test_launcher_refuses_symlinked_or_foreign_socket_dir(tmp_path):
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", tmp_path / "authz")
    r = _launch(tmp_path, "--degraded")
    assert r.returncode == 4 and "symlink" in r.stderr


def test_launcher_check_and_static_shape(tmp_path):
    r = _launch(tmp_path, "--check")
    assert "ptrace_scope=1" in r.stdout and "sock_dir=" in r.stdout
    text = LAUNCHER.read_text()
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "set -euo pipefail" in code and "$HOME" not in text and "~/" not in code
    assert "sudo" not in code          # the launcher is what sudo runs; it never calls sudo
    assert os.access(LAUNCHER, os.X_OK)


def _crew_authz_uid():
    import pwd
    try:
        return pwd.getpwnam("crew-authz").pw_uid
    except KeyError:
        pytest.skip("crew-authz user not present on this host")
