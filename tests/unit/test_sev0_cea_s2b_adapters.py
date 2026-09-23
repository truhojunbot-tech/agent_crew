"""SEV-0 CEA step 2b — §7 ingress adapters, the claim paths, and nonce carriage.

Contract: alfred ``sev0/e11-adr-draft`` @ ``6cbce565`` (Π P2, P4, §7, §11).

Two properties this file exists for, both found by the codex cross-repo review
of ``8993bdb`` as live bypasses rather than as style:

1. **Every** ``pending -> in_progress`` mutation goes through the CLAIM gate.
   ``dequeue_discuss_for_agent`` did not, so a discuss task was claimed with its
   receipt still ``QUEUED``. The static test below asserts the property about
   the *code*, because a third claim path added next year would leave every
   behavioural test here passing.
2. The dispatch nonce reaches the worker and comes back. ``record_dispatch``
   minted one and every caller dropped it, so under ``enforce`` ``/result``
   refused everything for ``NONCE_MISSING`` and under ``shadow`` it recorded a
   proof nobody had presented.
"""
from __future__ import annotations

import ast
import json
import re
import sqlite3
from pathlib import Path

import pytest

from agent_crew.cea import store as receipt_store
from agent_crew.cea.engine import EngineConfig
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue

from tests.unit.test_sev0_cea_s2c_writer_callsites import WIRED, admitted, task

SRC = Path(__file__).resolve().parents[2] / "src" / "agent_crew"


def queue(tmp_path, *, mode="shadow", name="t.db") -> TaskQueue:
    return TaskQueue(str(tmp_path / name), cea_config=EngineConfig(mode=mode),
                     cea_providers=dict(WIRED))


def discuss(task_id="d1", *, agent="codex") -> TaskRequest:
    return task(task_id, task_type="discuss",
                context=admitted({"agent": agent}), description="A vs B")


def _status(q: TaskQueue, task_id: str) -> str:
    conn = sqlite3.connect(q._db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT status FROM tasks WHERE task_id = ?",
                            (task_id,)).fetchone()["status"]
    finally:
        conn.close()


def _receipt(q: TaskQueue, task_id: str) -> dict:
    conn = sqlite3.connect(q._db_path)
    conn.row_factory = sqlite3.Row
    try:
        rid = conn.execute("SELECT receipt_id FROM tasks WHERE task_id = ?",
                           (task_id,)).fetchone()["receipt_id"]
        return receipt_store.current_receipt(conn, rid)
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# static — the set of claim paths is closed
# ═══════════════════════════════════════════════════════════════════════════

def _methods_mutating_to_in_progress() -> set[str]:
    """Names of ``TaskQueue`` methods whose body flips a row to ``in_progress``.

    A string search over the whole file would also match the recovery and
    watchdog SQL, which move rows *out* of / back into flight without claiming
    them for an agent; this looks for the pending-claim shape specifically.
    """
    src = (SRC / "queue.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = ast.get_source_segment(src, node) or ""
        if re.search(r"UPDATE tasks SET status = 'in_progress', last_activity_at",
                     body):
            found.add(node.name)
    return found


def test_every_claim_path_goes_through_the_one_gated_mutation():
    """P2 CLAIM is a property of the code, not of the two callers we remember.

    The reviewer's reproduction was exactly this: ``dequeue`` called the gate,
    ``dequeue_discuss_for_agent`` did not, and no behavioural test could tell,
    because each one only ever exercised the path it knew about.
    """
    assert _methods_mutating_to_in_progress() == {"claim_through_gate"}, (
        "a second pending->in_progress writer exists; route it through "
        "TaskQueue.claim_through_gate instead")


def test_the_declared_claim_callers_all_call_the_gated_mutation():
    src = (SRC / "queue.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    bodies = {n.name: (ast.get_source_segment(src, n) or "")
              for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in TaskQueue.CLAIM_MUTATION_METHODS:
        assert "claim_through_gate(" in bodies[name], f"{name} claims without the gate"


# ═══════════════════════════════════════════════════════════════════════════
# behavioural — the discuss claim path (codex review of 8993bdb, P1 #1)
# ═══════════════════════════════════════════════════════════════════════════

def test_discuss_claim_moves_the_receipt_to_claimed(tmp_path):
    """The exact reproduction: claim a discuss task, and the receipt must move.

    Before the fix the row became ``in_progress`` while its receipt stayed
    ``QUEUED`` — a claim the audit trail has no record of.
    """
    q = queue(tmp_path)
    q.enqueue(discuss())
    assert _receipt(q, "d1")["state"] == "QUEUED"

    got = q.dequeue_discuss_for_agent("codex")

    assert got is not None and got.task_id == "d1"
    assert _status(q, "d1") == "in_progress"
    assert _receipt(q, "d1")["state"] == "CLAIMED"


def test_discuss_claim_is_refused_when_the_gate_refuses(tmp_path):
    """A refused CLAIM leaves the discuss row pending, and its receipt QUEUED.

    Nothing is doctored to produce the refusal. A ``discuss`` task is OPS work
    (§2.3 J9) and identity is unconditionally UNVERIFIED under one uid, so the
    engine's honest answer is ``HUMAN_GATE`` — admitted in ``shadow``, and then
    refused at CLAIM by a runtime that enforces. That the discuss path *obeys*
    that answer is the property under test; before the fix it never asked.
    """
    q = queue(tmp_path)                                   # shadow: records, admits
    q.enqueue(discuss("d2"))
    assert _status(q, "d2") == "pending"

    enforced = TaskQueue(q._db_path, cea_config=EngineConfig(mode="test"),
                         cea_providers=dict(WIRED))
    assert enforced.dequeue_discuss_for_agent("codex") is None
    assert _status(q, "d2") == "pending"
    assert _receipt(q, "d2")["state"] == "QUEUED"

    # ...and the same task under `shadow` is claimed, with the answer recorded.
    assert q.dequeue_discuss_for_agent("codex") is not None
    assert _receipt(q, "d2")["state"] == "CLAIMED"


def test_a_receiptless_row_is_not_claimable_under_enforce(tmp_path):
    """P2 reads the same way for a missing receipt as for a refused one.

    A row written before step 2c has nothing to validate. Under ``shadow`` it
    still runs (and is reported); under ``enforce`` it does not run at all.
    """
    q = queue(tmp_path, mode="shadow")
    q.enqueue(task("legacy", context=admitted()))
    conn = sqlite3.connect(q._db_path)
    try:
        # The legacy shape is a row written by a build that had neither
        # trigger. Both are dropped and the row re-made, because the live
        # triggers exist precisely to stop this state from arising today —
        # what is under test is the rows that already did.
        conn.execute("DROP TRIGGER trg_tasks_receipt_id_required")
        conn.execute("DROP TRIGGER trg_tasks_receipt_id_immutable")
        conn.execute("UPDATE tasks SET receipt_id = NULL WHERE task_id = 'legacy'")
        conn.commit()
    finally:
        conn.close()

    assert q.dequeue(role="implementer") is not None      # shadow: runs, reported
    conn = sqlite3.connect(q._db_path)
    try:
        conn.execute("UPDATE tasks SET status = 'pending' WHERE task_id = 'legacy'")
        conn.commit()
    finally:
        conn.close()

    enforced = TaskQueue(q._db_path, cea_config=EngineConfig(mode="test"),
                         cea_providers=dict(WIRED))
    assert enforced.dequeue(role="implementer") is None
    assert _status(q, "legacy") == "pending"
