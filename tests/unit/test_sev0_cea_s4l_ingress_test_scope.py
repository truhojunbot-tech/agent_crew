"""An ingress cannot lower the test gate (step 4l, invariant 7, §7.2).

Codex acceptance [0] at c18e092 (P1): the dispatch path honoured
``task.context['test_scope'] == 'targeted'`` straight from the request, so any
caller that could enqueue a test task could switch off the full suite. J7's
``review_test_matrix``, read once at admission, is the only review/test
decision; §7.2 says nothing arriving through an ingress reduces it.

So at dispatch a reduced scope is honoured only when the task's admission
receipt carries it (``receipt.extra['j7_test_scope']``). A request-side
``test_scope`` / ``test_scope_source`` is logged and ignored, and the
attribution row names the scope that was actually used.

⛔The frozen receipt schema (ADR §0.3) has no J7 test-scope field yet — it is
  closed, so a stored receipt cannot carry one. Today that means every
  reduction is ignored and full scope holds, which is the fail-closed answer.
  The "honoured" tests stand in a fake engine at the receipt store so the read
  side is pinned before an ADR revision adds the field.

``test`` mode refuses a policy-less admission outright (403 BLOCK), which is
itself "the ingress cannot lower the gate"; so does its CLAIM gate. To
exercise *dispatch* under ``test`` the row is admitted and claimed in shadow
and dispatched with the mode flipped.
"""

import asyncio
import json
import logging
import os

import pytest

from agent_crew import testing_policy as tp
from agent_crew.cea import store as cea_store
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue

FULL = {"full_suite": True, "full": ["make test"]}


def _dispatch(tmp_path, monkeypatch, *, mode, context=None, receipt_extra=None,
              task_id="test-s4l"):
    """One real `_dispatch_task` for a test task; returns (row, events)."""
    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    async def _fake_exec(*cmd, **kwargs):
        class _P:
            returncode, pid = 0, 1

            async def wait(self):
                return 0

        return _P()

    wt = tmp_path / "worktrees" / "demo" / "gemini"
    wt.mkdir(parents=True, exist_ok=True)
    (wt / tp.REPO_SCOPE_FILE).parent.mkdir(parents=True, exist_ok=True)
    (wt / tp.REPO_SCOPE_FILE).write_text(json.dumps(FULL))
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"port": 8112, "worktrees": {"gemini": str(wt)}}))
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "shadow")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setenv("AGENT_CREW_BASE", str(tmp_path / "lb"))
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec", _fake_exec)

    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, pane_map={}, port=8112, state_path=str(state),
                     project="demo", watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        resp = client.post("/tasks", json={
            "task_id": task_id, "task_type": "test", "description": "run",
            "branch": "main", "project": "demo", "context": context or {}})
        assert resp.status_code < 300, resp.text
        q = TaskQueue(db)
        if receipt_extra is not None:
            _engine_writes_j7(monkeypatch, q, task_id, receipt_extra)
        task = q.dequeue(role="tester")
        assert task is not None and task.task_id == task_id
        monkeypatch.setenv("AGENT_CREW_CEA_MODE", mode)
        asyncio.run(app.state.dispatch_task(task, "tester"))
        row = q.get_attribution(task_id)
    events_path = os.path.join(os.path.dirname(db), "context_events.jsonl")
    events = ([json.loads(line) for line in open(events_path)]
              if os.path.exists(events_path) else [])
    return row, [e for e in events if e.get("event_type") == "test_scope_resolved"]


def _engine_writes_j7(monkeypatch, q, task_id, extra):
    """Fake engine: this task's receipt, as read back, carries a J7 scope field."""
    conn = q._connect()
    try:
        receipt_id, receipt = q._cea_receipt_for_task_on(conn, task_id)
    finally:
        conn.close()
    assert receipt is not None, "admission minted no receipt"
    real = cea_store.current_receipt

    def _current(c, rid):
        got = real(c, rid)
        if rid == receipt_id and isinstance(got, dict):
            got = {**got, "extra": {**(got.get("extra") or {}), **extra}}
        return got

    monkeypatch.setattr(cea_store, "current_receipt", _current)


def test_the_frozen_schema_cannot_store_a_j7_scope_yet(tmp_path):
    """Why the honoured path is a fake: a real receipt revision carrying the
    field is refused by the store, so production keeps full scope."""
    q = TaskQueue(str(tmp_path / "t.db"))
    q.enqueue(TaskRequest(task_id="t-frozen", task_type="test", description="run",
                          branch="main", project="demo"))
    conn = q._connect()
    try:
        _, receipt = q._cea_receipt_for_task_on(conn, "t-frozen")
        assert receipt is not None
        with pytest.raises(cea_store.ReceiptStoreError):
            cea_store.record_receipt(conn, {**receipt, "extra": {"j7_test_scope": "targeted"}})
    finally:
        conn.close()
    assert q.cea_admitted_test_scope("t-frozen") is None


@pytest.mark.parametrize("mode", ["shadow", "test"])
@pytest.mark.parametrize("source", [None, "task", "risk_tier", "j7"])
def test_a_request_supplied_targeted_scope_is_ignored(tmp_path, monkeypatch, caplog,
                                                     mode, source):
    ctx = {"test_scope": "targeted"}
    if source is not None:
        ctx["test_scope_source"] = source
    with caplog.at_level(logging.WARNING, logger="agent_crew.server"):
        row, resolved = _dispatch(tmp_path, monkeypatch, mode=mode, context=ctx)
    assert row["effective_test_scope"] == "full_suite"
    assert row["test_scope_source"] == "repo"
    assert row["test_scope_source"] != "task"
    assert resolved and resolved[0]["effective_test_scope"] == "full_suite"
    assert any("ingress test_scope ignored (§7.2)" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("mode", ["shadow", "test"])
def test_without_a_request_or_receipt_reduction_the_configured_scope_holds(
        tmp_path, monkeypatch, caplog, mode):
    with caplog.at_level(logging.WARNING, logger="agent_crew.server"):
        row, _ = _dispatch(tmp_path, monkeypatch, mode=mode)
    assert row["effective_test_scope"] == "full_suite"
    assert not any("ingress test_scope ignored" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("mode", ["shadow", "test"])
def test_a_receipt_carried_j7_reduction_is_honoured(tmp_path, monkeypatch, mode):
    row, resolved = _dispatch(
        tmp_path, monkeypatch, mode=mode,
        receipt_extra={"j7_test_scope": "targeted",
                       "j7_test_scope_source": "review_test_matrix"})
    assert row["effective_test_scope"] == "targeted"
    assert row["test_scope_source"] == "review_test_matrix"
    assert resolved[0]["effective_test_scope"] == "targeted"


def test_the_receipt_wins_over_what_the_request_claimed(tmp_path, monkeypatch):
    """Source is the receipt's J7 name, never the request's label."""
    row, _ = _dispatch(
        tmp_path, monkeypatch, mode="shadow",
        context={"test_scope": "targeted", "test_scope_source": "operator-says-so"},
        receipt_extra={"j7_test_scope": "targeted"})
    assert row["effective_test_scope"] == "targeted"
    assert row["test_scope_source"] == "j7"


def test_a_receipt_j7_field_naming_anything_else_is_not_a_reduction(tmp_path, monkeypatch):
    row, _ = _dispatch(tmp_path, monkeypatch, mode="shadow",
                       receipt_extra={"j7_test_scope": "skip"})
    assert row["effective_test_scope"] == "full_suite"


def test_a_task_with_no_receipt_keeps_full_scope(tmp_path):
    q = TaskQueue(str(tmp_path / "t.db"))
    assert q.cea_admitted_test_scope("never-existed") is None
