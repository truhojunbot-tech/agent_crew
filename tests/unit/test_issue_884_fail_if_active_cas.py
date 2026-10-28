"""#884: _fail_if_active must not overwrite a result that lands in flight.

_fail_if_active reads the in_progress list, then writes a terminal result.
A worker's completed result can commit between those two steps; the
dispatcher's write must then be refused, not replace it with timed_out.
"""

import sqlite3
import sys

from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _claimed(queue):
    queue.enqueue(TaskRequest(task_id="orphan", task_type="implement",
                              description="work", branch="main"))
    with sqlite3.connect(queue._db_path) as conn:
        conn.execute(
            "UPDATE tasks SET status='in_progress', claim_source='dispatcher', "
            "last_activity_at=900, dispatched_at=900, dispatch_agent='codex', "
            "dispatch_target='pid:123', lease_owner='dispatcher:old', "
            "lease_expires_at=1000.0, last_heartbeat_at=NULL WHERE task_id='orphan'")


def _app(db):
    return create_app(db, pane_map={}, pane_busy_fn=lambda _: False,
                      pane_liveness_fn=lambda _: "alive",
                      timeout_seconds=100000, watchdog_disabled=True,
                      anomaly_disabled=True)


def test_completed_result_landing_after_check_is_not_overwritten(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    queue = TaskQueue(tmp_db)
    _claimed(queue)

    original_list = TaskQueue.list_tasks
    injected = []

    def racing_list(self, *args, **kwargs):
        snapshot = original_list(self, *args, **kwargs)
        # Only race the read inside _fail_if_active, after it has its snapshot.
        if (not injected and kwargs.get("status") == "in_progress"
                and sys._getframe(1).f_code.co_name == "_fail_if_active"):
            injected.append(True)
            TaskQueue(tmp_db).submit_result("orphan", TaskResult(
                task_id="orphan", status="completed", summary="worker done"))
        return snapshot

    monkeypatch.setattr(TaskQueue, "list_tasks", racing_list)
    app = _app(tmp_db)

    with TestClient(app):
        actions = app.state.watchdog_tick(now=4000.0)

    assert injected, "race was not exercised"
    assert queue.get_task_status("orphan") == "completed"
    assert actions["timed_out"] == []
    with sqlite3.connect(tmp_db) as conn:
        (summary,) = conn.execute(
            "SELECT summary FROM tasks WHERE task_id='orphan'").fetchone()
    assert summary == "worker done"


def test_still_in_progress_task_is_timed_out(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    queue = TaskQueue(tmp_db)
    _claimed(queue)
    app = _app(tmp_db)

    with TestClient(app):
        actions = app.state.watchdog_tick(now=4000.0)

    assert actions["timed_out"] == ["orphan"]
    assert queue.get_task_status("orphan") == "timed_out"
