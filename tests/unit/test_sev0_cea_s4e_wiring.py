"""SEV-0 CEA step 4e — production wiring of the P1 input providers.

Contract: alfred ``sev0/e11-adr-draft`` @ ``6cbce565`` (Π P1, P6, P7, §3, §5, §6, §7,
§11); receipt schema alfred ``sev0/cea-alfred-lineage`` @ ``e1063eb``.

The defect these tests close is not a wrong decision, it is an *absent input*:
nothing in production constructed a CEA provider, so the engine decided
correctly on nothing and every receipt was BLOCK, while
``set_default_runtime_authority`` had no production call site at all.

Two things are therefore asserted here that no earlier suite could:

1. with nothing configured the factory wires nothing, says why for each slot,
   and — crucially — an unconfigured shadow runtime still enqueues and
   dispatches exactly as it did before (a BLOCK receipt is recorded, not raised);
2. with real files on disk the same factory produces providers that answer, the
   engine reaches ALLOW through them, and a *signed* snapshot restores ACTIVE
   through the production authority path rather than a test fake.

Nothing here touches a live server or the live governance files: every path is a
``tmp_path`` fixture.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from agent_crew.cea import wiring
from agent_crew.cea.engine import AuthorizationEngine, EngineConfig
from agent_crew.cea.input_providers.snapshot import CanonicalPolicySnapshotReader
from agent_crew.cea.intent import (
    CallerProvenance, Intent, IntentIdentity, Target, WorkClass)
from agent_crew.cea.providers import SignatureStatus
from agent_crew.cea.schema import validate_receipt

DECISION_ID = "T0-4E-1"
KEY = b"s4e-snapshot-key"

# A capability_id is deliberately absent: the E4 answer below says COVERED with
# no declared capability, which is the "plain intent" the task asks about — no
# reuse, no owner conflict, nothing that turns on the principal.
PROJECT, REPO, BASE_REF = "agent_crew", "example/agent_crew", "main"
ANCHORS = ("src/agent_crew/server.py",)


# ── fixtures on disk ────────────────────────────────────────────────────────

def _canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def write_snapshot(tmp_path: Path, *, signed=True, principals=("owner:hojun",),
                   build_commits=("b" * 40,), runtimes=("agent_crew",),
                   produced_at=None) -> Path:
    """A §5.3 snapshot file, HMAC-signed the way the reader's verifier checks."""
    when = produced_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    body = {
        "generation": 12,
        "produced_at": when,
        "tier": "T0",
        "decisions": [{
            "decision_id": DECISION_ID,
            "body_hash": "sha256:" + "a" * 32,
            "supersedes": [],
            "principals": list(principals),
            "build_commits": list(build_commits),
            "runtimes": list(runtimes),
            "scope": {"project": "agent_crew"},
        }],
        "review_test_matrix": {},
        "human_gate_predicates": [],
    }
    doc = dict(body)
    if signed:
        doc["signature"] = {
            "alg": "hmac-sha256",
            "key_id": hashlib.sha256(KEY).hexdigest()[:16],
            "value": hmac.new(KEY, _canonical(body), hashlib.sha256).hexdigest(),
        }
    path = tmp_path / "control_policy_snapshot.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def write_key(tmp_path: Path) -> Path:
    path = tmp_path / "snapshot.key"
    path.write_bytes(KEY)
    return path


def write_registry(tmp_path: Path) -> Path:
    path = tmp_path / "capability_registry.json"
    path.write_text(json.dumps({"generation": 1, "capabilities": []}), encoding="utf-8")
    return path


def write_memory_cmd(tmp_path: Path, *, status="OK") -> Path:
    """A stand-in for alfred ``tools/admission_inputs.py``.

    It speaks the real ``admission-inputs/v1`` contract over stdin/stdout — the
    point is to exercise *our* client and adapters, not alfred's matcher.
    """
    path = tmp_path / "admission_inputs.py"
    path.write_text(
        "import json, os, sys\n"
        "json.loads(sys.stdin.read())\n"
        "print(json.dumps({\n"
        "  'schema': 'admission-inputs/v1', 'status': %r,\n"
        "  'capability': {'provider': 'E4', 'status': %r,\n"
        "                 'registry': {'status': %r, 'generation': 1,\n"
        "                              'hash': 'sha256:fixture-registry-g1',\n"
        "                              'source': os.environ.get('AGENT_CREW_CEA_CAPABILITY_REGISTRY', '')},\n"
        "                 'matches': [], 'coverage': 'COVERED'},\n"
        "  'incident_memory': {'provider': 'incident_memory', 'status': 'OK',\n"
        "                      'index': {'generation': 1}, 'matches': []}}))\n"
        % (status, status, status),
        encoding="utf-8")
    return path


def write_quota(tmp_path: Path, *, utilization=0.1) -> Path:
    quota = tmp_path / "quota"
    for provider in ("claude", "codex", "gemini"):
        d = quota / f"{provider}_monitor"
        d.mkdir(parents=True, exist_ok=True)
        (d / "quota_cache.json").write_text(
            json.dumps({"fetched_at": time.time(),
                        "five_hour": {"utilization": utilization}}), encoding="utf-8")
    return quota


def absent_env(tmp_path: Path) -> dict:
    """Every CEA path pointed at something that is not there.

    ⛔Not literally "all env unset": the live box this runs on *has*
      ``/home/truhojun/alfred/governance/control_policy_snapshot.json``, so an
      empty env would find the real file and the assertion would pass or fail
      depending on the machine. Pointing every knob at an absent path under
      ``tmp_path`` tests the same branch and says the same thing deterministically.
    """
    missing = tmp_path / "absent"
    return {
        "AGENT_CREW_CEA_SNAPSHOT_PATH": str(missing / "snapshot.json"),
        "AGENT_CREW_CEA_REGISTRY_PATH": str(missing / "registry.json"),
        "AGENT_CREW_CEA_MEMORY_CMD": f"python3 {missing / 'admission_inputs.py'}",
        "AGENT_CREW_CEA_QUOTA_CACHE_DIR": str(missing / "quota"),
    }


def configured_env(tmp_path: Path, *, signed=True, key=True, **snap) -> dict:
    env = {
        "AGENT_CREW_CEA_SNAPSHOT_PATH": str(write_snapshot(tmp_path, signed=signed, **snap)),
        "AGENT_CREW_CEA_REGISTRY_PATH": str(write_registry(tmp_path)),
        "AGENT_CREW_CEA_MEMORY_CMD": f"{sys.executable} {write_memory_cmd(tmp_path)}",
        "AGENT_CREW_CEA_QUOTA_CACHE_DIR": str(write_quota(tmp_path)),
    }
    if key:
        env["AGENT_CREW_CEA_SNAPSHOT_KEY_FILE"] = str(write_key(tmp_path))
    return env


def queue_db(tmp_path: Path) -> str:
    """A real queue database, so the P6 row the runtime provider reads exists."""
    from agent_crew.queue import TaskQueue

    path = str(tmp_path / "tasks.db")
    TaskQueue(path)
    return path


def intent(task_id="s4e-1", *, work_class=WorkClass.REVIEW, authority=(DECISION_ID,)) -> Intent:
    return Intent(
        identity=IntentIdentity(
            project=PROJECT, work_class=work_class,
            target=Target(repo=REPO, base_ref=BASE_REF, scope_anchors=ANCHORS),
            capability_id=None, authority_decision_ids=tuple(authority)),
        task_id=task_id, task_type="review", description="review the dispatch path")


def caller():
    from agent_crew.cea.auth import AdapterIdentity, StaticTokenAuthenticator

    token = "s4e::cron"
    who = StaticTokenAuthenticator(
        {token: AdapterIdentity(principal="cron:admitted_trigger",
                                provenance=CallerProvenance.CRON)}).authenticate(token)
    assert who is not None
    return who


@pytest.fixture
def conn():
    from agent_crew.cea import store as receipt_store

    c = sqlite3.connect(":memory:")
    receipt_store.ensure_schema(c)
    try:
        yield c
    finally:
        c.close()


# ── 1. nothing configured ───────────────────────────────────────────────────

def test_nothing_configured_wires_nothing_and_says_why_for_every_slot(tmp_path):
    w = wiring.build_wiring(absent_env(tmp_path), db_path=None, project="agent_crew")

    assert w.providers == {}, "an unreadable input must not produce a provider object"
    assert w.wired_slots == ()
    assert w.authority is None, "no snapshot ⇒ RefuseAllLoosening stays"
    for slot in wiring.SLOTS + ("l3_memory",):
        status = w.status(slot)
        assert status is not None, f"{slot} must be reported, not omitted"
        assert status.state == wiring.UNAVAILABLE
        assert status.reason, f"{slot} UNAVAILABLE with no reason is what made this undebuggable"
    assert "mode=shadow" in w.log_line()


def test_an_unwired_engine_blocks_on_unavailable_inputs_rather_than_guessing(tmp_path, conn):
    """P7 through the *factory*: what the wiring failed to build, the engine reports."""
    built, w = wiring.build_engine_from_env(absent_env(tmp_path), db_path=None,
                                            project="agent_crew")
    assert isinstance(built, AuthorizationEngine), "no endpoint ⇒ the in-process engine"
    assert w.providers == {}
    # mode=test is `enforce` minus the deployment requirement: the verdict is
    # identical in every mode, and here it is the verdict that is under test.
    engine = AuthorizationEngine(config=EngineConfig(mode="test"), **w.providers)

    auth = engine.authorize(conn, intent(), caller())
    assert auth.decision == "BLOCK"
    assert auth.receipt["reason"]["code"] == "INPUTS_UNAVAILABLE"
    assert "did not answer" in auth.receipt["reason"]["text"]
    validate_receipt(auth.receipt)          # a BLOCK is still a contract-valid receipt


def test_every_provider_path_missing_never_raises_into_organic_dispatch(tmp_path, monkeypatch):
    """(4) ``mode=shadow`` must not be able to break the queue.

    The strongest version of "missing" is not an absent file — it is a provider
    that *explodes*. So each slot here is wired to a class that raises on every
    call, and the queue must still enqueue, claim and finish the task.
    """
    from agent_crew.queue import TaskQueue, TaskRequest

    class Boom:
        def __getattr__(self, _name):
            def raise_it(*a, **kw):
                raise RuntimeError("provider exploded")
            return raise_it

    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "shadow")
    q = TaskQueue(str(tmp_path / "tasks.db"),
                  cea_providers={slot: Boom() for slot in wiring.SLOTS})
    q.enqueue(TaskRequest(task_id="organic-1", task_type="implement",
                          description="unrelated work", project="agent_crew"),
              ingress="cli.enqueue")

    db = sqlite3.connect(str(tmp_path / "tasks.db"))
    rows = db.execute("SELECT task_id, status FROM tasks").fetchall()
    assert rows == [("organic-1", "pending")], "organic enqueue must be unaffected"

    receipts = db.execute("SELECT decision, reason FROM authorization_receipts").fetchall()
    db.close()
    assert receipts, "shadow must still record what it decided"
    assert {r[0] for r in receipts} == {"BLOCK"}
    assert {json.loads(r[1])["code"] for r in receipts} == {"INPUTS_UNAVAILABLE"}, \
        "a provider that raised is an unavailable input, not a 500"


# ── 2. configured from real files ───────────────────────────────────────────

def test_configured_paths_wire_every_provider(tmp_path):
    w = wiring.build_wiring(configured_env(tmp_path), db_path=queue_db(tmp_path),
                            project="agent_crew")

    assert sorted(w.providers) == sorted(wiring.SLOTS), w.log_line()
    assert set(w.wired_slots) == set(wiring.SLOTS) | {"l3_memory"}, w.log_line()
    assert "hmac-sha256 verified" in w.status("snapshots").reason


def test_a_plain_intent_is_allowed_through_the_wired_providers(tmp_path, conn):
    """The whole point: a receipt that says ALLOW because inputs *answered*.

    Every one of the five inputs here comes from a file or a subprocess through
    the production factory — no fake providers, no injected doubles.
    """
    engine, w = wiring.build_engine_from_env(
        configured_env(tmp_path), db_path=queue_db(tmp_path), project="agent_crew")
    engine = AuthorizationEngine(config=EngineConfig(mode="test"), **w.providers)

    auth = engine.authorize(conn, intent(), caller())
    assert auth.decision == "ALLOW", auth.receipt["reason"]
    assert auth.http_status == 201
    assert auth.receipt["binding"]["runtime_state"] == "ACTIVE"
    validate_receipt(auth.receipt)


def test_an_unsigned_snapshot_is_an_unverified_input_not_a_lenient_one(tmp_path, conn):
    """Leaving the key file unset must not quietly buy permissiveness."""
    env = configured_env(tmp_path, key=False)
    w = wiring.build_wiring(env, db_path=queue_db(tmp_path), project="agent_crew")
    assert w.status("snapshots").wired
    assert "unkeyed" in w.status("snapshots").reason
    assert w.authority is None

    engine = AuthorizationEngine(config=EngineConfig(mode="test"), **w.providers)
    auth = engine.authorize(conn, intent(), caller())
    assert auth.decision == "BLOCK"
    assert auth.receipt["reason"]["code"] == "INPUTS_UNAVAILABLE"


# ── 3. the P6 restoration path ──────────────────────────────────────────────

def test_a_verified_t0_record_restores_active_through_the_production_reader(tmp_path):
    """The proof the plan could not make: a *file* on disk restores ACTIVE.

    Before this step the only granting authority in the tree read a fake
    snapshot provider built in ``conftest``; the production reader dropped
    ``principals`` / ``build_commits`` / ``runtimes``, so no real snapshot could
    ever satisfy conditions 3 and 4.
    """
    from agent_crew.queue import TaskQueue

    build = "c" * 40
    env = configured_env(tmp_path, principals=("owner:hojun",), build_commits=(build,),
                         runtimes=("agent_crew",))
    w = wiring.build_wiring(env, db_path=None, project="agent_crew")
    assert w.authority is not None, w.authority_reason
    w.authority._build_commit = build     # this process is not the contained build

    q = TaskQueue(str(tmp_path / "p6.db"), runtime_authority=w.authority)
    q.transition_runtime_state("QUARANTINED", who="owner:hojun", reason="s4e drill")
    assert q.get_runtime_state()["state"] == "QUARANTINED"

    q.transition_runtime_state("ACTIVE", who="owner:hojun", decision_id=DECISION_ID,
                               reason="restore under a signed T0 record")
    assert q.get_runtime_state()["state"] == "ACTIVE"


def test_the_same_restoration_is_refused_when_the_snapshot_is_not_signed(tmp_path):
    """Condition 1: an unverified snapshot is an unavailable input, not a lenient one."""
    from agent_crew.queue import RuntimeTransitionRefused, SnapshotLooseningAuthority, TaskQueue

    env = configured_env(tmp_path, signed=False, key=True)
    reader = CanonicalPolicySnapshotReader(env["AGENT_CREW_CEA_SNAPSHOT_PATH"],
                                           verifier=None, env=env)
    assert reader.current().signature is not SignatureStatus.VALID

    # Hand the authority the reader directly — the strongest form of the claim:
    # even wired, an unsigned snapshot cannot loosen.
    q = TaskQueue(str(tmp_path / "p6b.db"),
                  runtime_authority=SnapshotLooseningAuthority(reader, build_commit="c" * 40,
                                                               runtime="agent_crew"))
    q.transition_runtime_state("QUARANTINED", who="owner:hojun", reason="s4e drill")
    with pytest.raises(RuntimeTransitionRefused, match="not VALID"):
        q.transition_runtime_state("ACTIVE", who="owner:hojun", decision_id=DECISION_ID)


def test_install_never_removes_an_authority_a_surrounding_process_installed(tmp_path):
    """``set_default_runtime_authority(None)`` is not a no-op — it is a downgrade.

    So "we found nothing to install" must leave the existing default alone.
    """
    from agent_crew import queue as queue_mod

    before = queue_mod._DEFAULT_RUNTIME_AUTHORITY
    assert before is not None, "conftest installs the granting verifier for the suite"
    w = wiring.install_from_env(absent_env(tmp_path), db_path=None, project="agent_crew")
    assert w.authority is None
    assert queue_mod._DEFAULT_RUNTIME_AUTHORITY is before


def test_install_wires_the_authority_when_the_snapshot_verifies(tmp_path):
    from agent_crew import queue as queue_mod

    before = queue_mod._DEFAULT_RUNTIME_AUTHORITY
    try:
        w = wiring.install_from_env(configured_env(tmp_path), db_path=None, project="agent_crew")
        assert w.authority is not None
        assert queue_mod._DEFAULT_RUNTIME_AUTHORITY is w.authority
    finally:
        queue_mod.set_default_runtime_authority(before)


# ── 4. deployment shape ─────────────────────────────────────────────────────

def test_an_engine_endpoint_means_this_process_wires_nothing(tmp_path):
    """§7.1: with ``crew-authz`` configured the deciding process is elsewhere.

    Building providers here would furnish a process that does not decide.
    """
    env = dict(configured_env(tmp_path),
               AGENT_CREW_CEA_ENGINE_ENDPOINT="/tmp/crew-authz.sock")
    w = wiring.build_wiring(env, db_path=queue_db(tmp_path), project="agent_crew")
    assert w.providers == {}
    assert w.endpoint == "/tmp/crew-authz.sock"
    assert all("remote" in s.reason for s in w.statuses)


def test_the_server_constructs_its_queue_with_the_wired_providers():
    """The Codex finding was about *construction*, so this is a source assertion.

    A passing provider test proves the factory works; only the call site proves
    production uses it.
    """
    import inspect

    from agent_crew import server

    src = inspect.getsource(server.create_app)
    assert "cea_wiring.install_from_env(" in src
    assert "TaskQueue(db_path, cea_providers=" in src, \
        "the server must hand the wiring to the queue, not build a provider-less one"
