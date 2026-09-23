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


# ═══════════════════════════════════════════════════════════════════════════
# transport — the dispatch nonce reaches the worker and comes back
# (codex review of 8993bdb, P1 #2)
# ═══════════════════════════════════════════════════════════════════════════

PANE = "%crew-s2b"


class RecordingPush:
    def __init__(self):
        self.calls = []

    def __call__(self, target, message):
        self.calls.append((target, message))


@pytest.fixture
def push():
    return RecordingPush()


@pytest.fixture
def client(tmp_path, push):
    from fastapi.testclient import TestClient
    from agent_crew.server import create_app
    db = str(tmp_path / "srv.db")
    TaskQueue(db)                                   # schema, before the app binds
    app = create_app(db, pane_map={"implementer": PANE}, port=8105, push_fn=push,
                     watchdog_disabled=True, anomaly_disabled=True)
    # The app builds its queue on startup, so the context manager is load-bearing.
    with TestClient(app) as api:
        yield api, db


def _enqueue(db, task_id="t-nonce", **kw):
    q = TaskQueue(db, cea_config=EngineConfig(mode="shadow"), cea_providers=dict(WIRED))
    q.enqueue(task(task_id, context=admitted(), **kw))
    return q


def test_the_tmux_task_block_carries_the_nonce(client, push, tmp_path):
    """The block the pane actually receives names the nonce and the /start call.

    Before this, ``record_dispatch`` minted a nonce and ``_try_push_next``
    dropped it — so the worker had nothing to present and the two later call
    sites had nothing to check.
    """
    from agent_crew.server import _format_task_message

    _, db = client
    q = _enqueue(db)
    got = q.dequeue(role="implementer")
    nonce = q.record_dispatch(got.task_id, channel="tmux_pane", agent="claude",
                              target=PANE)
    assert nonce, "a dispatch through an admitted receipt mints a nonce"

    block = _format_task_message(got, 8105, nonce=nonce)

    assert f"dispatch_nonce: {nonce}" in block
    assert f"/tasks/{got.task_id}/start" in block
    assert f'"executor_binding":{{"nonce":"{nonce}"}}' in block


def test_a_task_with_no_nonce_gets_the_unchanged_block(client):
    """``nonce=None`` reproduces the pre-2b block: no /start step, no binding.

    A block that told a worker to present a nonce it was never given would move
    the same lie one hop downstream.
    """
    from agent_crew.server import _format_task_message

    _, db = client
    q = _enqueue(db, task_id="t-plain")
    got = q.dequeue(role="implementer")

    block = _format_task_message(got, 8105, nonce=None)

    assert "dispatch_nonce" not in block
    assert "/start" not in block
    assert "executor_binding" not in block


def test_http_poll_hands_the_nonce_to_the_worker(client):
    """``GET /tasks/next`` returns ``dispatch_nonce`` alongside the task."""
    api, db = client
    _enqueue(db, task_id="t-http")

    body = api.get("/tasks/next", params={"role": "implementer"}).json()

    assert body["task_id"] == "t-http"
    assert body["dispatch_nonce"], "the poller cannot present what it was not given"


def test_http_result_presents_the_nonce_and_it_is_not_persisted(client):
    """End to end over HTTP: poll → start → result, with the same nonce.

    Two properties at once. The RESULT gate must *see* the nonce — until now it
    saw ``None`` on every result, so ``enforce`` refused everything for
    ``NONCE_MISSING``. And the spent credential must not land in ``result_json``.
    """
    api, db = client
    _enqueue(db, task_id="t-e2e")

    handed = api.get("/tasks/next", params={"role": "implementer"}).json()
    nonce = handed["dispatch_nonce"]

    go = api.post("/tasks/t-e2e/start", json={"nonce": nonce, "presenter": "claude"}).json()
    assert go["go"] is True
    assert go["nonce_spent"] is True

    api.post("/tasks/t-e2e/result", json={
        "task_id": "t-e2e", "status": "completed", "summary": "done",
        "executor_binding": {"nonce": nonce, "presenter": "claude"}})

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id='t-e2e'").fetchone()
    finally:
        conn.close()
    gate = json.loads(row["context"])["cea_result"]
    assert gate["point"] == "result"
    assert "NONCE_MISSING" not in gate["reason"], gate
    stored = " ".join(str(row[k]) for k in row.keys())
    assert nonce not in stored, "a spent credential stayed in the task row"


def test_mcp_hands_out_and_accepts_the_same_nonce(tmp_path):
    """The MCP transport carries the nonce both ways, like the HTTP one.

    ⛔Both transports or neither. A gate that only one transport can pass is a
      gate an agent walks around by changing how it polls (#123).
    """
    from agent_crew import mcp_server

    db = str(tmp_path / "mcp.db")
    TaskQueue(db)
    _enqueue(db, task_id="t-mcp")
    queue = TaskQueue(db)

    got = queue.dequeue(agent="claude", role="implementer", claimed_via="mcp")
    nonce = queue.record_dispatch(got.task_id, channel="api", agent="claude",
                                  target="mcp:claude")
    payload = mcp_server._task_to_dict(got, nonce=nonce)
    assert payload["dispatch_nonce"] == nonce

    # ...and the result side accepts it and strips it.
    from agent_crew.protocol import TaskResult
    result = TaskResult(task_id="t-mcp", status="completed", summary="done",
                        executor_binding={"nonce": nonce, "presenter": "claude"})
    seen_nonce, seen_presenter = result.take_executor_binding()
    assert (seen_nonce, seen_presenter) == (nonce, "claude")
    assert result.executor_binding is None
    queue.submit_result("t-mcp", result, nonce=seen_nonce, presenter=seen_presenter)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id='t-mcp'").fetchone()
    finally:
        conn.close()
    assert "NONCE_MISSING" not in json.loads(row["context"])["cea_result"]["reason"]
    assert nonce not in " ".join(str(row[k]) for k in row.keys())


def test_every_dispatch_caller_carries_the_nonce_somewhere():
    """Static: no caller may discard ``record_dispatch``'s return value.

    The reviewer's finding was not that one caller forgot — it was that *every*
    caller forgot, so the mint had no consumer anywhere in the product and no
    behavioural test could notice. A bare expression-statement call is exactly
    that shape.
    """
    offenders = []
    for path in (SRC / "server.py", SRC / "mcp_server.py", SRC / "pipeline.py",
                 SRC / "watch.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and node.value.func.attr == "record_dispatch"):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], (
        "record_dispatch's nonce is discarded at " + ", ".join(offenders)
        + " — the worker cannot present what it was never handed (P2 RESULT)")
