"""Owner 2026-10-21 / review of PR #818, round 1: ``off`` computes no verdict.

Before this, ``off`` ran the whole admission sweep — every provider read, the
verdict decided, ``BLOCK`` recorded — and the queue wrote the task row anyway.
That is observation-only operation under a mode whose contract is "no verdict is
computed", i.e. the forbidden shadow mode under another name.

Now the engine *stands down* in ``off``: it writes one receipt whose only reason
is ``CEA_MODE_OFF`` (the queue's ``tasks.receipt_id`` trigger needs a receipt on
every row, P2), consults nothing, claims no lineage, and the five call sites
record that they stood down instead of running the validator. ``test`` and
``enforce`` are untouched: the gate is on ``off`` alone.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from agent_crew.cea import store as receipt_store
from agent_crew.cea import wiring
from agent_crew.cea.callsites import gate_off
from agent_crew.cea.engine import (
    CEA_MODE_OFF, ENFORCEABLE_REASON_CODES, OFF, AuthorizationEngine, EngineConfig)
from agent_crew.cea.schema import validate_receipt
from agent_crew.cea.validator import ValidationPoint
from agent_crew.protocol import TaskResult
from agent_crew.queue import TaskQueue, TaskRequest

from tests.unit.test_sev0_cea_s2c_writer_callsites import admitted, task
from tests.unit.test_sev0_cea_s4e_wiring import caller, intent


class Consulted:
    """A provider that records every call and then explodes.

    The strongest form of "not consulted": if ``off`` touched any slot, the
    call is on the log *and* the caller sees an exception it did not expect.
    """

    def __init__(self, log: list, slot: str):
        self.__dict__["log"], self.__dict__["slot"] = log, slot

    def __getattr__(self, name):
        def hit(*a, **kw):
            self.log.append((self.slot, name))
            raise RuntimeError(f"{self.slot}.{name} consulted under off")
        return hit


def providers(log: list) -> dict:
    return {slot: Consulted(log, slot) for slot in wiring.SLOTS}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    receipt_store.ensure_schema(c)
    try:
        yield c
    finally:
        c.close()


def _receipts(db_path: str) -> list[tuple[str, str]]:
    db = sqlite3.connect(db_path)
    try:
        rows = db.execute("SELECT decision, reason FROM authorization_receipts "
                          "WHERE seq = 0").fetchall()
    finally:
        db.close()
    return [(d, json.loads(r)["code"]) for d, r in rows]


def _context(q: TaskQueue, task_id: str) -> dict:
    db = sqlite3.connect(q._db_path)
    try:
        return json.loads(db.execute("SELECT context FROM tasks WHERE task_id = ?",
                                     (task_id,)).fetchone()[0] or "{}")
    finally:
        db.close()


# ── the engine ──────────────────────────────────────────────────────────────

def test_the_stand_down_code_can_never_enforce():
    assert CEA_MODE_OFF not in ENFORCEABLE_REASON_CODES


def test_off_writes_one_stand_down_receipt_and_consults_nothing(conn):
    log: list = []
    engine = AuthorizationEngine(config=EngineConfig(mode=OFF), **providers(log))

    auth = engine.authorize(conn, intent(), caller())

    assert log == [], "off must not read a single provider"
    assert auth.decision == "ALLOW" and auth.code == CEA_MODE_OFF
    assert auth.http_status == 201
    r = auth.receipt
    assert r["reason"]["code"] == CEA_MODE_OFF
    assert "no admission verdict was computed" in r["reason"]["text"]
    assert r["policy_hash"] == "not-consulted"
    assert r["binding"]["policy_hash"] == "not-consulted"
    assert r["matched_capability"] is None and r["required_reviewer"] is None
    assert r["signature"]["status"] == "UNVERIFIED" and r["downgrade_reason"]
    assert validate_receipt(r) == [], "the stand-down is still a contract-valid receipt"
    assert conn.execute("SELECT COUNT(*) FROM authorization_receipts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM intent_lineages").fetchone()[0] == 0, \
        "off claims no lineage: it is not a judgement about the work"


def test_off_does_not_canonicalize_or_refuse(conn):
    """No project named is a refusal in a computing mode (PROJECT_REQUIRED). In
    ``off`` nothing is judged, so nothing is refused: the receipt names the
    unnamed-project sentinel and stands down like any other."""
    from dataclasses import replace
    log: list = []
    engine = AuthorizationEngine(config=EngineConfig(mode=OFF), **providers(log))
    nameless = intent()
    nameless = replace(nameless, identity=replace(nameless.identity, project=""))
    auth = engine.authorize(conn, nameless, caller())
    assert auth.code == CEA_MODE_OFF and auth.receipt["project"] == "cea/unnamed-project"
    assert log == []


def test_off_still_requires_an_authenticated_caller(conn):
    """J9's 401 is not a verdict: an unauthenticated caller gets no audit row in
    a name it chose, in any mode."""
    from agent_crew.cea.engine import UnauthenticatedCaller
    from tests.unit.test_sev0_cea_engine import forged_caller
    engine = AuthorizationEngine(config=EngineConfig(mode=OFF))
    forged = forged_caller(principal="attacker")
    with pytest.raises(UnauthenticatedCaller):
        engine.authorize(conn, intent(), forged)
    assert conn.execute("SELECT COUNT(*) FROM authorization_receipts").fetchone()[0] == 0


def test_test_mode_still_computes_the_real_verdict(conn):
    """The gate is on ``off`` only. In ``test`` the providers are read and an
    exploding provider is an unavailable input, as before."""
    log: list = []
    engine = AuthorizationEngine(config=EngineConfig(mode="test"), **providers(log))
    auth = engine.authorize(conn, intent(), caller())
    assert log, "test mode consults its inputs"
    assert auth.decision == "BLOCK" and auth.code == "INPUTS_UNAVAILABLE"


# ── the gate helper ─────────────────────────────────────────────────────────

def test_gate_off_proceeds_unenforced_and_names_a_refusal_on_file():
    plain = gate_off(ValidationPoint.ENQUEUE, {"receipt_id": "r1", "decision": "ALLOW",
                                               "reason": {"code": CEA_MODE_OFF}})
    assert plain.proceed is True and plain.enforced is False
    assert plain.outcome.value == "PROCEED" and plain.receipt_id == "r1"
    assert plain.reason.startswith(f"{CEA_MODE_OFF}: ")
    assert "receipt on file" not in plain.reason
    assert "advisory" not in plain.as_record() and "review_required" not in plain.as_record()

    refused = gate_off(ValidationPoint.ENQUEUE, {"receipt_id": "r2", "decision": "BLOCK",
                                                 "reason": {"code": "PROJECT_MISMATCH"}})
    assert refused.proceed is True and refused.enforced is False, \
        "a queue-side refusal was advisory in off before; enforcement is not widened"
    assert "BLOCK/PROJECT_MISMATCH" in refused.reason and "not acted on" in refused.reason


# ── end to end through the queue ────────────────────────────────────────────

def test_off_admission_end_to_end(tmp_path):
    """The reviewer's reproduction, inverted: organic enqueue under ``off`` with
    providers that explode on contact. One pending row, one receipt, and the
    receipt is the stand-down — not a BLOCK the row then ignores."""
    log: list = []
    db = str(tmp_path / "tasks.db")
    q = TaskQueue(db, cea_config=EngineConfig(mode=OFF), cea_providers=providers(log))

    q.enqueue(TaskRequest(task_id="organic-1", task_type="implement",
                          description="unrelated work", project="agent_crew"),
              ingress="cli.enqueue")

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT task_id, status FROM tasks").fetchall() == \
            [("organic-1", "pending")]
        receipt_id = conn.execute("SELECT receipt_id FROM tasks").fetchone()[0]
        current = receipt_store.current_receipt(conn, receipt_id)
    finally:
        conn.close()
    assert _receipts(db) == [("ALLOW", CEA_MODE_OFF)], "exactly one receipt, and it stood down"
    assert current["state"] == "QUEUED", "the ISSUED → QUEUED lifecycle still runs"
    record = _context(q, "organic-1")["cea_enqueue"]
    assert record["outcome"] == "PROCEED" and record["proceed"] is True
    assert record["enforced"] is False and record["receipt_id"] == receipt_id
    assert record["reason"].startswith(f"{CEA_MODE_OFF}: ")
    assert "advisory" not in record
    assert log == [], f"off consulted providers at admission: {log}"


def test_off_lifecycle_never_consults_a_provider_and_never_records_a_verdict(tmp_path):
    """claim → dispatch → execute-start → result, all under ``off``: every call
    site stands down, the nonce protocol still runs, nothing is computed."""
    log: list = []
    db = str(tmp_path / "life.db")
    q = TaskQueue(db, cea_config=EngineConfig(mode=OFF), cea_providers=providers(log))
    q.enqueue(task("t-off", context=admitted()), ingress="http.tasks")

    got = q.dequeue(role="implementer", agent="claude")
    assert got is not None and got.task_id == "t-off"
    nonce = q.record_dispatch("t-off", channel="tmux_pane", agent="claude", target="%1")
    assert nonce, "dispatch still mints the executor-binding nonce in off"

    answer = q.start_execution("t-off", nonce, presenter="claude")
    assert answer["go"] is True and answer["enforced"] is False
    assert answer["outcome"] == "PROCEED" and answer["nonce_spent"] is True
    assert answer["reason"].startswith(f"{CEA_MODE_OFF}: ")
    assert "advisory" not in answer and "shadow_outcome" not in answer, \
        "nothing was computed, so there is no verdict to label advisory"

    q.submit_result("t-off", TaskResult(task_id="t-off", status="completed", summary="done"),
                    nonce=nonce, presenter="claude")
    ctx = _context(q, "t-off")
    assert ctx["cea_result"]["outcome"] == "PROCEED" and ctx["cea_result"]["enforced"] is False
    assert ctx["cea_result"]["reason"].startswith(f"{CEA_MODE_OFF}: ")
    conn = sqlite3.connect(db)
    try:
        status = conn.execute("SELECT status FROM tasks WHERE task_id='t-off'").fetchone()[0]
        receipt_id = conn.execute("SELECT receipt_id FROM tasks WHERE task_id='t-off'").fetchone()[0]
        final = receipt_store.current_receipt(conn, receipt_id)
    finally:
        conn.close()
    assert status == "completed"
    assert final["state"] == "CONSUMED", "the lifecycle is kept; only the verdict is gone"
    assert _receipts(db) == [("ALLOW", CEA_MODE_OFF)]
    assert log == [], f"off consulted providers somewhere in the lifecycle: {log}"


def test_a_stand_down_receipt_does_not_carry_work_into_enforce(tmp_path):
    """Switching a project to ``enforce`` must not let rows admitted while off
    run on their stand-down receipt: it is unsigned and not-consulted, and the
    enforce claim path refuses an unverifiable receipt."""
    db = str(tmp_path / "flip.db")
    TaskQueue(db, cea_config=EngineConfig(mode=OFF)).enqueue(
        task("t-flip", context=admitted()), ingress="http.tasks")
    assert _receipts(db) == [("ALLOW", CEA_MODE_OFF)]

    enforcing = TaskQueue(db, cea_config=EngineConfig(mode="enforce"))
    assert enforcing.dequeue(role="implementer", agent="claude") is None
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT status FROM tasks WHERE task_id='t-flip'").fetchone()[0] \
            == "pending"
    finally:
        conn.close()
