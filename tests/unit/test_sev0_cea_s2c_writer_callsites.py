"""SEV-0 CEA step 2c — the sole ``QUEUED`` writer and the five validator call sites.

Contract: alfred ``sev0/e11-adr-draft`` @ ``6cbce565`` (Π P2, P3, P4, P6, P7,
§3, §7, §11.2). The engine's own decision table, ``intent_hash`` invariance and
the duplicate/replay/completed rules are covered against the engine in
``test_sev0_cea_engine.py``; this file is about what happens when the *queue*
is the caller — i.e. whether the properties survive the adapter.

Two of these tests are **static**. P2's claim is not "the validator is correct",
it is "there is no enqueue, no claim, no dispatch, no execute without a valid
receipt" — a claim about *where the code calls it from*, which no runtime test
can establish. A second ``INSERT INTO tasks`` added next year would leave every
behavioural test passing and the property gone.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from agent_crew.cea import callsites, store as receipt_store
from agent_crew.cea.engine import EngineConfig
from agent_crew.cea.intent import WorkClass
from agent_crew.cea.providers import CapabilityLookup, PolicySnapshotRef, SignatureStatus
from agent_crew.cea.receipt import (
    BudgetClass, DecisionRev, HumanGate, HumanGateState, MatchedCapability,
    ProviderBudget, RegistryRef)
from agent_crew.cea.runtime_state import RuntimeState, RuntimeStateSnapshot
from agent_crew.cea.schema import validate_receipt
from agent_crew.cea.validator import ValidationPoint
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import AdmissionRefused, TaskQueue, intent_for_task

SRC = Path(__file__).resolve().parents[2] / "src" / "agent_crew"


def _sources() -> dict[Path, str]:
    return {p: p.read_text(encoding="utf-8") for p in sorted(SRC.rglob("*.py"))}


# ---------------------------------------------------------------------------
# providers — wired ones, so a test that wants an admitted task can have one.
# Same shape as the engine suite's: every input answers unless asked otherwise.
# ---------------------------------------------------------------------------

DECISION = DecisionRev(decision_id="T0-1234", body_hash="b" * 32)


class Snapshot:
    def current(self, intent=None) -> PolicySnapshotRef:
        return PolicySnapshotRef(generation=7, hash="h" * 16, produced_at=None,
                                 decisions=(DECISION,), in_scope=(DECISION,),
                                 signature=SignatureStatus.VALID, available=True, tier="T0")


class Registry:
    def lookup(self, intent) -> CapabilityLookup:
        return CapabilityLookup(registry=RegistryRef(generation="2026-09-23.1", hash="r" * 16),
                                matches=(), anchor_matches=(), available=True, stale=False)


class Runtime:
    def current(self):
        return RuntimeStateSnapshot(state=RuntimeState.ACTIVE, epoch=3, read_failed=False)


class Budget:
    def budget(self, provider):
        return ProviderBudget(provider=provider, state=BudgetClass.OK, observed_at=None)


class Gate:
    def state(self, intent, snapshot):
        return HumanGate(HumanGateState.NOT_REQUIRED)


WIRED = {"snapshots": Snapshot(), "capabilities": Registry(), "runtime": Runtime(),
         "budgets": Budget(), "gates": Gate()}


def queue(tmp_path, *, mode="shadow", wired=False, name="t.db") -> TaskQueue:
    return TaskQueue(str(tmp_path / name), cea_config=EngineConfig(mode=mode),
                     cea_providers=(dict(WIRED) if wired else None))


def task(task_id="t1", *, task_type="implement", context=None, project="agent_crew",
         description="add a --json flag", branch="main") -> TaskRequest:
    return TaskRequest(task_id=task_id, task_type=task_type, description=description,
                       branch=branch, priority=3, context=dict(context or {}), project=project)


def admitted(context=None) -> dict:
    """Context for work the wired snapshot actually authorises (J2)."""
    ctx = {"authority_decision_ids": ["T0-1234"], "repo": "example/agent_crew"}
    ctx.update(context or {})
    return ctx


def row(q: TaskQueue, task_id="t1") -> sqlite3.Row:
    conn = sqlite3.connect(q._db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    finally:
        conn.close()


def receipt_of(q: TaskQueue, task_id="t1") -> dict:
    conn = sqlite3.connect(q._db_path)
    conn.row_factory = sqlite3.Row
    try:
        rid = conn.execute("SELECT receipt_id FROM tasks WHERE task_id = ?",
                           (task_id,)).fetchone()["receipt_id"]
        return receipt_store.current_receipt(conn, rid)
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# I2 — static: one writer, one validator, five call sites
# ═══════════════════════════════════════════════════════════════════════════

def test_there_is_exactly_one_writer_of_task_rows():
    """P2: a task row is the physical form of an admission decision.

    ⛔The property is "one writer", not "the writer checks". A second
      ``INSERT INTO tasks`` anywhere in the product is a second admission path,
      and it would be invisible to every behavioural test in this file.
    """
    # The column list is part of the pattern on purpose: prose mentioning the
    # statement (this rule is documented in the code it constrains) is not a
    # writer, and a test that counted those would be a tripwire for editing a
    # docstring.
    statement = re.compile(r"INSERT\s+INTO\s+tasks\s*\(", re.IGNORECASE)
    writers = {path: len(statement.findall(body)) for path, body in _sources().items()
               if statement.search(body)}
    assert writers == {SRC / "queue.py": 1}, (
        f"exactly one INSERT INTO tasks, in queue.py; found {writers}")
    body = (SRC / "queue.py").read_text(encoding="utf-8")
    writer = body[body.index("def enqueue_with_receipt"):body.index("def enqueue(self, task")]
    assert statement.search(writer), \
        "the one INSERT INTO tasks must live in enqueue_with_receipt"


def test_there_is_exactly_one_receipt_validator_in_the_product():
    """P2/T3: "one receipt validator implementation ... four call sites"."""
    built = {path: len(re.findall(r"ContractReceiptValidator\(\)", body))
             for path, body in _sources().items() if "ContractReceiptValidator()" in body}
    assert built == {SRC / "cea" / "callsites.py": 1}, (
        f"one validator instance, in cea/callsites.py; found {built}")


def test_each_of_the_five_validate_methods_has_exactly_one_call_site():
    sources = _sources()
    for method in ("validate_enqueue", "validate_claim", "validate_dispatch",
                   "validate_execute_start", "validate_result"):
        callers = {path: len(re.findall(rf"VALIDATOR\.{method}\(", body))
                   for path, body in sources.items() if f"VALIDATOR.{method}(" in body}
        assert callers == {SRC / "cea" / "callsites.py": 1}, \
            f"{method} must be called from exactly one place; found {callers}"


def test_the_five_points_are_exactly_the_five_call_sites():
    assert set(callsites.CALL_SITES) == set(ValidationPoint)
    assert len({id(fn) for fn in callsites.CALL_SITES.values()}) == 5


def test_no_module_outside_the_cea_package_calls_the_validator_directly():
    """The gates in ``callsites`` are where shadow/enforce is decided. A caller
    reaching past them into ``validator.validate`` would be enforcing a policy of
    its own — which is the P1 violation the whole design exists to prevent."""
    offenders = {path.name for path, body in _sources().items()
                 if "cea/" not in path.as_posix().split("agent_crew/")[-1]
                 and re.search(r"\bvalidator\.validate\w*\(", body)}
    assert not offenders, f"these call the validator directly: {sorted(offenders)}"


# ═══════════════════════════════════════════════════════════════════════════
# tasks.receipt_id — required at the database, not at the application
# ═══════════════════════════════════════════════════════════════════════════

def test_a_task_row_without_a_receipt_is_refused_by_the_database(tmp_path):
    q = queue(tmp_path)
    conn = sqlite3.connect(q._db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError) as exc:
            conn.execute("INSERT INTO tasks (task_id, task_type, description, branch, "
                         "priority, context, status, created_at, project) "
                         "VALUES ('x', 'implement', 'd', 'main', 3, '{}', 'pending', 0, 'p')")
        assert "receipt_id is required" in str(exc.value)
    finally:
        conn.close()


def test_a_receipt_id_cannot_be_cleared_or_repointed(tmp_path):
    q = queue(tmp_path)
    q.enqueue(task())
    conn = sqlite3.connect(q._db_path)
    try:
        for value in (None, "", "some-other-receipt"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE tasks SET receipt_id = ? WHERE task_id = 't1'", (value,))
    finally:
        conn.close()


def test_every_enqueued_row_names_the_receipt_that_admitted_it(tmp_path):
    q = queue(tmp_path)
    q.enqueue(task())
    r = row(q)
    assert r["receipt_id"]
    stored = receipt_of(q)
    assert stored["task_id"] == "t1"
    assert stored["state"] == "QUEUED", "the row exists, so the receipt is QUEUED (§3)"


def test_owner_conflict_is_parked_and_readmitted_only_on_new_signed_generation(
        tmp_path, monkeypatch):
    from agent_crew.cea.engine import intent_hash
    owner_match = MatchedCapability(id="shared.owner-capability", owner="other_project",
                                    repo="example/other_project")

    class OwnerRegistry(Registry):
        def lookup(self, intent):
            return CapabilityLookup(registry=RegistryRef(generation="1", hash="r" * 16),
                                    matches=(owner_match,))

    class OwnerSnapshot(Snapshot):
        generation = 7
        grant = None

        def current(self, intent=None):
            decisions = (DECISION,) + ((self.grant,) if self.grant else ())
            return PolicySnapshotRef(generation=self.generation, hash=f"h{self.generation}" * 8,
                                     produced_at=None, decisions=decisions, in_scope=decisions,
                                     signature=SignatureStatus.VALID, available=True, tier="T0")

    class OwnerRuntime(Runtime):
        state = RuntimeState.ACTIVE

        def current(self):
            return RuntimeStateSnapshot(state=self.state, epoch=3, read_failed=False)

    class OwnerBudget(Budget):
        state = BudgetClass.OK

        def budget(self, provider):
            return ProviderBudget(provider=provider, state=self.state, observed_at=None)

    snapshot = OwnerSnapshot()
    runtime = OwnerRuntime()
    budget = OwnerBudget()
    q = TaskQueue(str(tmp_path / "t.db"), cea_config=EngineConfig(mode="test"),
                  cea_providers={**WIRED, "snapshots": snapshot,
                                 "capabilities": OwnerRegistry(), "runtime": runtime,
                                 "budgets": budget})
    import subprocess
    requests = []
    original_run = subprocess.run
    def record_owner_request(*args, **kwargs):
        if args and args[0] == ["owner-request"]:
            requests.append((args, kwargs))
            return subprocess.CompletedProcess(args[0], 0)
        return original_run(*args, **kwargs)
    monkeypatch.setenv("AGENT_CREW_CEA_OWNER_REQUEST_CMD", "owner-request")
    monkeypatch.setattr("agent_crew.queue.subprocess.run", record_owner_request)
    t = task(context=admitted({"pr_number": 594}))
    q.enqueue(t)
    assert q.get_task("t1").status == "needs_human"
    assert q.parked_owner_conflict_count() == 1
    assert receipt_of(q)["state"] == "HELD"
    assert q.external_op_get(f"owner-approval:{receipt_of(q)['intent_hash']}")["state"] == "done"
    q._request_owner_approval(t, receipt_of(q))
    assert len(requests) == 1
    request = json.loads(requests[0][1]["input"])
    assert request["project"] == "agent_crew"
    assert request["capability_id"] == owner_match.id
    assert request["capability_owner"] == owner_match.owner
    assert request["task_id"] == "t1"
    assert request["pr_number"] == 594
    from fastapi.testclient import TestClient
    import agent_crew.server as server
    from agent_crew.server import create_app
    monkeypatch.setattr(server, "TaskQueue", lambda *args, **kwargs: q)
    with TestClient(create_app(q._db_path, pane_map={}, project="agent_crew",
                               watchdog_disabled=True, anomaly_disabled=True)) as client:
        health_cea = client.get("/health").json()["cea"]
        assert health_cea["owner_conflicts_parked"] == 1
        assert health_cea["owner_approval_requests"] == {"total": 1, "failed": 0}
        held = client.post("/tasks", json={"task_id": "t2", "task_type": "implement",
                                           "description": "another owner conflict", "branch": "feature",
                                           "context": admitted({"pr_number": 594}),
                                           "project": "agent_crew"})
        assert held.status_code == 423
        assert held.json()["reason"] == "OWNER_CONFLICT"
        assert q.get_task("t2").status == "needs_human"
        assert q.parked_owner_conflict_count() == 2
    assert q.readmit_parked_owner_conflicts() == []
    snapshot.generation = 8
    assert q.readmit_parked_owner_conflicts() == []
    assert q.get_task("t1").status == "needs_human"
    assert q.get_task("t1").context["owner_conflict"]["last_attempt_generation"] == 8
    with sqlite3.connect(q._db_path) as connection:
        receipts_at_gen8 = connection.execute(
            "SELECT COUNT(DISTINCT receipt_id) FROM authorization_receipts").fetchone()[0]
    assert q.readmit_parked_owner_conflicts() == []
    with sqlite3.connect(q._db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(DISTINCT receipt_id) FROM authorization_receipts").fetchone()[0] == receipts_at_gen8
    snapshot.generation = 9
    snapshot.grant = DecisionRev("OWNER-594", "c" * 32,
                                 capabilities=(f"reuse:{owner_match.id}",),
                                 projects=(t.project,),
                                 intent_hash=intent_hash(intent_for_task(t, context=t.context,
                                                                         queue_identity=t.project).identity))
    restarted = TaskQueue(q._db_path, cea_config=EngineConfig(mode="test"),
                          cea_providers={**WIRED, "snapshots": snapshot,
                                         "capabilities": OwnerRegistry(), "runtime": runtime,
                                         "budgets": budget})
    import agent_crew.queue as queue_module
    now = [queue_module.time.time()]
    monkeypatch.setattr(queue_module.time, "time", lambda: now[0])
    original_enqueue = restarted.enqueue_with_receipt
    enqueue_calls = []
    failed_once = False

    def transient_enqueue_failure(successor, receipt, **kwargs):
        nonlocal failed_once
        enqueue_calls.append(successor.task_id)
        if successor.task_id == "owner-readmit-t1-g9" and not failed_once:
            failed_once = True
            raise RuntimeError("transient enqueue failure")
        return original_enqueue(successor, receipt, **kwargs)

    monkeypatch.setattr(restarted, "enqueue_with_receipt", transient_enqueue_failure)
    runtime.state = RuntimeState.STOPPED
    assert restarted.readmit_parked_owner_conflicts() == []
    with sqlite3.connect(q._db_path) as connection:
        receipts_during_stop = connection.execute(
            "SELECT COUNT(DISTINCT receipt_id) FROM authorization_receipts").fetchone()[0]
    for _ in range(10):
        assert restarted.readmit_parked_owner_conflicts() == []
    with sqlite3.connect(q._db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(DISTINCT receipt_id) FROM authorization_receipts").fetchone()[0] == receipts_during_stop
    assert enqueue_calls == []  # A signed BLOCK must never reach the row writer.
    assert q.get_task("t1").context["owner_conflict"]["last_attempt_generation"] == 8
    assert q.get_task("owner-readmit-t1-g9") is None
    runtime.state = RuntimeState.ACTIVE
    assert restarted.readmit_parked_owner_conflicts() == []  # still in backoff
    now[0] += restarted.OWNER_CONFLICT_RETRY_SECONDS + 1
    original_authorize = restarted.authorize_task
    auth_failed_once = False

    def transient_authorize_failure(successor, **kwargs):
        nonlocal auth_failed_once
        if successor.task_id == "owner-readmit-t1-g9" and not auth_failed_once:
            auth_failed_once = True
            raise RuntimeError("transient authorization failure")
        return original_authorize(successor, **kwargs)

    monkeypatch.setattr(restarted, "authorize_task", transient_authorize_failure)
    assert restarted.readmit_parked_owner_conflicts() == []
    assert auth_failed_once
    assert enqueue_calls == []
    assert q.get_task("t1").context["owner_conflict"]["last_attempt_generation"] == 8
    now[0] += restarted.OWNER_CONFLICT_RETRY_SECONDS + 1
    assert restarted.readmit_parked_owner_conflicts() == []
    assert failed_once
    assert q.get_task("t1").status == "needs_human"
    assert q.get_task("t1").context["owner_conflict"]["last_attempt_generation"] == 8
    now[0] += restarted.OWNER_CONFLICT_RETRY_SECONDS + 1
    assert restarted.readmit_parked_owner_conflicts() == ["owner-readmit-t1-g9"]
    assert q.get_task("t1").status == "cancelled"
    assert q.get_task("owner-readmit-t1-g9").status == "pending"
    assert q.parked_owner_conflict_count() == 1  # t2 still awaits its own approval
    t2 = q.get_task("t2")
    snapshot.generation = 10
    snapshot.grant = DecisionRev("OWNER-594-t2", "d" * 32,
                                 capabilities=(f"reuse:{owner_match.id}",),
                                 projects=(t2.project,),
                                 intent_hash=intent_hash(intent_for_task(
                                     t2, context=t2.context,
                                     queue_identity=t2.project).identity))
    budget.state = BudgetClass.EXHAUSTED
    assert restarted.readmit_parked_owner_conflicts() == []
    assert q.get_task("t2").context["owner_conflict"]["last_attempt_generation"] == 10
    with sqlite3.connect(q._db_path) as connection:
        receipts_at_budget_block = connection.execute(
            "SELECT COUNT(DISTINCT receipt_id) FROM authorization_receipts").fetchone()[0]
    now[0] += restarted.OWNER_CONFLICT_RETRY_SECONDS * 10
    for _ in range(10):
        assert restarted.readmit_parked_owner_conflicts() == []
    with sqlite3.connect(q._db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(DISTINCT receipt_id) FROM authorization_receipts").fetchone()[0] == receipts_at_budget_block


def test_owner_request_without_command_is_counted_once(tmp_path, monkeypatch, caplog):
    q = queue(tmp_path)
    monkeypatch.delenv("AGENT_CREW_CEA_OWNER_REQUEST_CMD", raising=False)
    receipt = {"intent_hash": "sha256:" + "a" * 64,
               "binding": {"matched_capability": {"id": "c", "owner": "owner"}}}
    q._request_owner_approval(task(), receipt)
    q._request_owner_approval(task(), receipt)
    assert q.owner_approval_request_counts() == {"total": 1, "failed": 0}
    assert "approval request unconfigured" in caplog.text


# ═══════════════════════════════════════════════════════════════════════════
# the legacy path reaches the engine — no bypass
# ═══════════════════════════════════════════════════════════════════════════

def test_the_legacy_enqueue_goes_through_the_engine_and_records_a_receipt(tmp_path):
    """The temporary internal call is the point: every existing ingress keeps
    working and starts producing receipts. If ``enqueue`` wrote a row without
    authorising, the shadow measurement would be of a system nobody is running."""
    q = queue(tmp_path)
    q.enqueue(task())
    stored = receipt_of(q)
    assert stored["issuer"] == "agent_crew.cea.engine"
    assert stored["caller_identity"].startswith("agent_crew.in_process:")
    assert stored["caller_identity_status"] == "UNVERIFIED"
    assert stored["downgrade_reason"] == "SHARED_UID_NO_CREDENTIAL_BOUNDARY"


def test_with_no_providers_the_receipt_says_so_instead_of_allowing(tmp_path):
    """P7 through the adapter: an unwired runtime does not shadow-ALLOW."""
    q = queue(tmp_path)
    q.enqueue(task())
    stored = receipt_of(q)
    assert stored["decision"] == "BLOCK"
    assert stored["reason"]["code"] == "INPUTS_UNAVAILABLE"
    recorded = json.loads(row(q)["context"])["cea_enqueue"]
    assert recorded["outcome"] == "BLOCK" and recorded["proceed"] is True
    assert recorded["enforced"] is False, "shadow records the answer and proceeds"


def test_every_receipt_the_queue_emits_validates_against_the_frozen_schema(tmp_path):
    """Including every lifecycle revision — the store validates on write, so a
    receipt that reached a row is a receipt that conformed at every step."""
    q = queue(tmp_path, wired=True)
    q.enqueue(task(task_type="review", context=admitted()))
    q.dequeue(agent="codex", role="reviewer")
    nonce = q.record_dispatch("t1", channel="tmux_pane", agent="codex", target="%1")
    q.start_execution("t1", nonce, presenter="codex")
    q.submit_result("t1", TaskResult(task_id="t1", status="completed", summary="done"),
                    nonce=nonce, presenter="codex")
    conn = sqlite3.connect(q._db_path)
    conn.row_factory = sqlite3.Row
    try:
        rid = row(q)["receipt_id"]
        history = receipt_store.receipt_history(conn, rid)
    finally:
        conn.close()
    assert [h["state"] for h in history][0] == "ISSUED"
    assert [h["state"] for h in history][-1] == "CONSUMED"
    for entry in history:
        assert validate_receipt(entry["receipt"]) == [], \
            f"seq {entry['seq']} violates the frozen contract"


# ═══════════════════════════════════════════════════════════════════════════
# shadow vs enforce — the same verdict, a different consequence
# ═══════════════════════════════════════════════════════════════════════════

def test_enforce_refuses_the_row_that_shadow_writes(tmp_path):
    shadow = queue(tmp_path, name="shadow.db")
    shadow.enqueue(task())
    assert row(shadow) is not None

    enforced = queue(tmp_path, mode="test", name="enforce.db")
    with pytest.raises(AdmissionRefused) as exc:
        enforced.enqueue(task())
    assert exc.value.point == "enqueue"
    assert row(enforced) is None, "a refused admission writes no task row (P2)"


def test_a_refused_enqueue_still_leaves_the_audit_row(tmp_path):
    """P2: the receipt exists even when the task row does not. Without it a
    refusal is indistinguishable from a request nobody made."""
    q = queue(tmp_path, mode="test")
    with pytest.raises(AdmissionRefused):
        q.enqueue(task())
    conn = sqlite3.connect(q._db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM authorization_receipts").fetchone()[0]
    finally:
        conn.close()
    assert n >= 1


def test_wired_inputs_admit_review_work_under_enforcement(tmp_path):
    """The gate is not simply "always refuse": with every input answering and an
    authority the snapshot carries, admission proceeds and the row is written."""
    q = queue(tmp_path, mode="test", wired=True)
    q.enqueue(task(task_type="review", context=admitted()))
    stored = receipt_of(q)
    assert stored["decision"] == "ALLOW", stored["reason"]
    assert json.loads(row(q)["context"])["cea_enqueue"]["outcome"] == "PROCEED"


# ═══════════════════════════════════════════════════════════════════════════
# P4 through the adapter — identity, duplicates, replay, completed work
# ═══════════════════════════════════════════════════════════════════════════

def test_the_queue_builds_the_same_intent_for_a_reworded_task():
    """P4: the description is not a member of the identity."""
    a = intent_for_task(task("t1", description="add a --json flag"))
    b = intent_for_task(task("t1", description="ADD A JSON FLAG, please"))
    assert a.identity == b.identity


def test_a_new_task_id_for_declared_work_builds_the_same_intent():
    """P4: the task_id is not a member of the identity either — for work the
    task *declared*, which is what CX-4c is the permanent record of.

    ⛔Deliberately declares its target. This assertion used to hold for a task
      that declared nothing, because `scope_anchors` and `repo` were then both
      empty for everything — which is how two unrelated organic tasks on one
      branch came to share an `intent_hash` and the second was refused
      DUPLICATE_INTENT (owner P1 alfred#51). `intent_for_task` now anchors an
      undeclared task on `task://<task_id>`; see
      ``tests/unit/test_sev0_cea_p1_scope_anchors.py`` for the derivation order
      and for the CX-4c case this test states.
    """
    declared = {"scope_anchors": ["src/agent_crew/cli.py"]}
    a = intent_for_task(task("t1", context=declared, description="add a --json flag"))
    b = intent_for_task(task("t2", context=declared, description="ADD A JSON FLAG, please"))
    assert a.identity == b.identity


@pytest.mark.parametrize("kw", [
    {"task_type": "review"},                                  # work_class
    {"branch": "release"},                                    # base_ref
    {"project": "other"},                                     # project
    {"context": {"repo": "example/other"}},                   # repo
    {"context": {"scope_anchors": ["src/agent_crew/cli.py"]}},  # scope anchors
    {"context": {"capability_id": "cap.x"}},                  # capability
    {"context": {"authority_decision_ids": ["T0-9999"]}},     # authority
])
def test_every_p4_member_the_queue_can_set_changes_the_intent(kw):
    base = intent_for_task(task())
    assert intent_for_task(task(**kw)).identity != base.identity


def test_an_unknown_task_type_takes_the_strictest_work_class():
    """A work class nobody declared must not be the one that needs no reviewer."""
    unknown = task()
    unknown.task_type = "something-new"     # past the protocol's own enum, on purpose
    assert intent_for_task(unknown).identity.work_class is WorkClass.IMPLEMENT


def test_the_same_intent_under_a_new_task_id_is_refused_as_a_duplicate(tmp_path):
    q = queue(tmp_path, mode="test", wired=True)
    q.enqueue(task("t1", task_type="review", context=admitted()))
    with pytest.raises(AdmissionRefused) as exc:
        q.enqueue(task("t2", task_type="review", context=admitted()))
    assert exc.value.outcome == "BLOCK"
    assert row(q, "t2") is None


def test_the_same_idempotency_key_returns_the_existing_receipt(tmp_path):
    q = queue(tmp_path, wired=True)
    ctx = admitted({"idempotency_key": "k1"})
    # Use an implement task: the separate standing-review guard rejects a
    # second review before this CEA idempotency assertion can inspect it.
    q.enqueue(task("t1", task_type="implement", context=ctx))
    first = row(q, "t1")["receipt_id"]
    q.enqueue(task("t2", task_type="implement", context=ctx))
    assert row(q, "t2")["receipt_id"] == first, \
        "a replayed request is the same work; it does not get a second receipt (P4)"


def test_completed_work_is_not_re_admitted(tmp_path):
    q = queue(tmp_path, mode="test", wired=True)
    ctx = admitted()
    q.enqueue(task("t1", task_type="review", context=ctx))
    q.dequeue(agent="codex", role="reviewer")
    nonce = q.record_dispatch("t1", channel="tmux_pane", agent="codex", target="%1")
    q.start_execution("t1", nonce, presenter="codex")
    q.submit_result("t1", TaskResult(task_id="t1", status="completed", summary="done"),
                    nonce=nonce, presenter="codex")
    assert receipt_of(q, "t1")["state"] == "CONSUMED"
    with pytest.raises(AdmissionRefused):
        q.enqueue(task("t2", task_type="review", context=ctx))


# ═══════════════════════════════════════════════════════════════════════════
# the four post-admission call sites
# ═══════════════════════════════════════════════════════════════════════════

def test_claim_refuses_a_claimant_that_is_not_the_bound_executor(tmp_path):
    """P2a: an asserted identity is tamper-evident, not proven — but a claimant
    that does not even match the binding is caught before it holds the row."""
    q = queue(tmp_path, mode="test", wired=True)
    q.enqueue(task(task_type="review", context=admitted()))
    assert receipt_of(q)["executor_binding"] == "codex"
    assert q.dequeue(agent="gemini", role="reviewer") is None
    assert row(q)["status"] == "pending", "a refused claim leaves the row claimable"
    assert q.dequeue(agent="codex", role="reviewer") is not None


def test_dispatch_mints_one_single_use_nonce_per_attempt(tmp_path):
    q = queue(tmp_path, wired=True)
    q.enqueue(task(task_type="review", context=admitted()))
    q.dequeue(agent="codex", role="reviewer")
    nonce = q.record_dispatch("t1", channel="tmux_pane", agent="codex", target="%1")
    assert nonce
    conn = sqlite3.connect(q._db_path)
    conn.row_factory = sqlite3.Row
    try:
        stored = receipt_store.nonce_row(conn, nonce)
    finally:
        conn.close()
    assert stored["receipt_id"] == row(q)["receipt_id"]
    assert stored["attempt"] == receipt_of(q)["attempt"]
    assert stored["used_at"] is None


def test_execute_start_spends_the_nonce_exactly_once(tmp_path):
    q = queue(tmp_path, mode="test", wired=True)
    q.enqueue(task(task_type="review", context=admitted()))
    q.dequeue(agent="codex", role="reviewer")
    nonce = q.record_dispatch("t1", channel="tmux_pane", agent="codex", target="%1")
    first = q.start_execution("t1", nonce, presenter="codex")
    assert first["go"] is True and first["nonce_spent"] is True
    second = q.start_execution("t1", nonce, presenter="codex")
    assert second["go"] is False, "P4: a dispatch nonce is single-use"


def test_unknown_consumed_status_keeps_allowlisted_replay_no_go(tmp_path):
    """An ALREADY_COMPLETED allowlist cannot admit unverified consumed work."""
    q = TaskQueue(
        str(tmp_path / "consumed-unknown.db"),
        cea_config=EngineConfig(
            mode="test", enforce_codes=frozenset({"ALREADY_COMPLETED"})),
        cea_providers=dict(WIRED),
    )
    q.enqueue(task("first", task_type="review", context=admitted()))
    q.dequeue(agent="codex", role="reviewer")
    nonce = q.record_dispatch("first", channel="tmux_pane", agent="codex", target="%1")
    assert q.start_execution("first", nonce, presenter="codex")["go"] is True
    q.submit_result("first", TaskResult(
        "first", "completed", "Reviewed and approved the full change successfully.",
        verdict="approve"), nonce=nonce, presenter="codex")
    assert receipt_of(q, "first")["state"] == "CONSUMED"
    # The task row is missing, so completion cannot be established from it.
    with sqlite3.connect(q._db_path) as conn:
        conn.execute("DELETE FROM tasks WHERE task_id='first'")

    with pytest.raises(AdmissionRefused) as refusal:
        q.enqueue(task("replay", task_type="review", context=admitted()))
    assert refusal.value.gate.proceed is False
    assert refusal.value.outcome == "BLOCK"
    assert q.get_task_status("replay") is None


def test_shadow_block_start_is_explicitly_advisory(tmp_path):
    q = queue(tmp_path, mode="shadow", wired=True)
    q.enqueue(task(task_type="review", context=admitted()))
    q.dequeue(agent="codex", role="reviewer")
    nonce = q.record_dispatch("t1", channel="tmux_pane", agent="codex", target="%1")
    answer = q.start_execution("t1", nonce, presenter="gemini")
    assert answer["go"] is True
    assert answer["outcome"] == "BLOCK"
    assert answer["enforced"] is False
    assert answer["advisory"] is True
    assert answer["shadow_outcome"] == "BLOCK"
    assert answer["instruction"] == "shadow mode: not enforced — proceed; decide on go only"


def _start_answer(tmp_path, mode):
    q = queue(tmp_path, mode=mode, wired=True, name=f"{mode}.db")
    q.enqueue(task(task_type="review", context=admitted()))
    q.dequeue(agent="codex", role="reviewer")
    nonce = q.record_dispatch("t1", channel="tmux_pane", agent="codex", target="%1")
    return q.start_execution("t1", nonce, presenter="gemini")


def test_shadow_start_reason_is_labelled_not_enforced(tmp_path):
    """#409: on go the reason prose must not read as an enforced refusal."""
    shadow = _start_answer(tmp_path, "shadow")
    enforced = _start_answer(tmp_path, "test")
    assert shadow["go"] is True and enforced["go"] is False
    assert shadow["reason"].startswith("shadow (not enforced): ")
    # Same gate verdict underneath: only the label differs, nothing is dropped.
    assert shadow["reason"] == "shadow (not enforced): " + enforced["reason"]
    assert shadow["outcome"] == enforced["outcome"] == "BLOCK"


def test_enforced_start_reason_carries_no_shadow_label(tmp_path):
    answer = _start_answer(tmp_path, "test")
    assert answer["go"] is False and answer["enforced"] is True
    assert "not enforced" not in answer["reason"]
    assert not answer["reason"].startswith(("shadow", "advisory"))


def test_enforced_block_start_response_is_unchanged(tmp_path):
    q = queue(tmp_path, mode="test", wired=True)
    q.enqueue(task(task_type="review", context=admitted()))
    q.dequeue(agent="codex", role="reviewer")
    nonce = q.record_dispatch("t1", channel="tmux_pane", agent="codex", target="%1")
    answer = q.start_execution("t1", nonce, presenter="gemini")
    assert json.dumps(answer, separators=(",", ":")) == json.dumps({
        "go": False, "task_id": "t1", "receipt_id": answer["receipt_id"],
        "outcome": "BLOCK", "reason": answer["reason"], "enforced": True,
        "nonce_spent": False,
    }, separators=(",", ":"))


def test_execute_start_refuses_a_nonce_nobody_minted(tmp_path):
    q = queue(tmp_path, mode="test", wired=True)
    q.enqueue(task(task_type="review", context=admitted()))
    q.dequeue(agent="codex", role="reviewer")
    q.record_dispatch("t1", channel="tmux_pane", agent="codex", target="%1")
    answer = q.start_execution("t1", "f" * 32, presenter="codex")
    assert answer["go"] is False and "NONCE" in answer["reason"]


def test_result_without_a_nonce_is_refused_under_enforce(tmp_path):
    """The pane protocol does not carry a nonce yet (step 2b). Under enforce
    that is a refusal, not a pass — which is exactly why enforce must wait."""
    q = queue(tmp_path, mode="test", wired=True)
    q.enqueue(task(task_type="review", context=admitted()))
    q.dequeue(agent="codex", role="reviewer")
    nonce = q.record_dispatch("t1", channel="tmux_pane", agent="codex", target="%1")
    q.start_execution("t1", nonce, presenter="codex")
    with pytest.raises(AdmissionRefused) as exc:
        q.submit_result("t1", TaskResult(task_id="t1", status="completed", summary="done"))
    assert exc.value.point == "result"
    assert row(q)["status"] == "in_progress", "a refused result is not recorded"


def test_result_refuses_a_presenter_that_is_not_the_bound_executor(tmp_path):
    q = queue(tmp_path, mode="test", wired=True)
    q.enqueue(task(task_type="review", context=admitted()))
    q.dequeue(agent="codex", role="reviewer")
    nonce = q.record_dispatch("t1", channel="tmux_pane", agent="codex", target="%1")
    q.start_execution("t1", nonce, presenter="codex")
    with pytest.raises(AdmissionRefused):
        q.submit_result("t1", TaskResult(task_id="t1", status="completed", summary="done"),
                        nonce=nonce, presenter="gemini")


def test_shadow_accepts_the_same_result_and_records_the_refusal(tmp_path):
    """The shadow measurement is only worth something if the answer is the same
    one enforce would have acted on."""
    q = queue(tmp_path, wired=True)
    q.enqueue(task(task_type="review", context=admitted()))
    q.dequeue(agent="codex", role="reviewer")
    q.record_dispatch("t1", channel="tmux_pane", agent="codex", target="%1")
    q.submit_result("t1", TaskResult(task_id="t1", status="completed", summary="done"))
    recorded = json.loads(row(q)["context"])["cea_result"]
    assert recorded["outcome"] == "BLOCK" and recorded["proceed"] is True
    assert "NONCE_MISSING" in recorded["reason"]
    assert row(q)["status"] == "completed"


# ═══════════════════════════════════════════════════════════════════════════
# B′ is computed, never assumed
# ═══════════════════════════════════════════════════════════════════════════

def test_b_prime_is_recomputed_over_the_scope_admission_used(tmp_path):
    q = queue(tmp_path, wired=True)
    q.enqueue(task(task_type="review", context=admitted(
        {"scope_anchors": ["src/agent_crew/server.py"]})))
    stored = receipt_of(q)
    identity = stored["provenance"]["intent_identity"]
    assert identity["target"]["scope_anchors"] == ["src/agent_crew/server.py"]
    binding, unavailable = q.cea_engine().current_binding(stored)
    assert unavailable == ()
    assert binding == stored["binding"], "B′ over the same scope is the same B"


def test_a_receipt_without_a_recorded_identity_reports_b_prime_unavailable(tmp_path):
    """⛔Never a guessed scope: a B′ computed over an invented identity compares
      unequal for reasons that have nothing to do with drift, and a validator
      cannot tell the two apart."""
    q = queue(tmp_path, wired=True)
    q.enqueue(task(task_type="review", context=admitted()))
    stored = dict(receipt_of(q))
    stored["provenance"] = {k: v for k, v in stored["provenance"].items()
                            if k != "intent_identity"}
    binding, unavailable = q.cea_engine().current_binding(stored)
    assert binding is None and unavailable == ("intent_identity",)
