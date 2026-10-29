"""Expired dispatcher leases are reaped only after owner and heartbeat checks."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _claimed(queue, *, lease_expires_at=1000.0, heartbeat_at=None):
    queue.enqueue(TaskRequest(task_id="orphan", task_type="implement",
                              description="work", branch="main"))
    with sqlite3.connect(queue._db_path) as conn:
        conn.execute(
            "UPDATE tasks SET status='in_progress', claim_source='dispatcher', "
            "last_activity_at=900, dispatched_at=900, dispatch_agent='codex', "
            "dispatch_target='pid:123', lease_owner='dispatcher:old', "
            "lease_expires_at=?, last_heartbeat_at=? WHERE task_id='orphan'",
            (lease_expires_at, heartbeat_at))


def _app(db, *, pane_map=None, busy=False):
    return create_app(db, pane_map=pane_map or {},
                      pane_busy_fn=lambda _: busy,
                      pane_liveness_fn=lambda _: "alive",
                      timeout_seconds=100000, watchdog_disabled=True,
                      anomaly_disabled=True)


def test_expired_orphan_times_out_once_and_frees_slot(tmp_db, monkeypatch, caplog):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    queue = TaskQueue(tmp_db)
    _claimed(queue)
    queue.enqueue(TaskRequest(task_id="next", task_type="implement",
                              description="next", branch="main"))
    app = _app(tmp_db)

    with TestClient(app):
        first = app.state.watchdog_tick(now=4000.0)
        second = app.state.watchdog_tick(now=4001.0)

    assert first["timed_out"] == ["orphan"]
    assert second["timed_out"] == []
    assert queue.get_task_status("orphan") == "timed_out"
    assert app.state.watchdog_expiry_counts["orphan_lease_expired"] == 1
    assert "reason=orphan_lease_expired" in caplog.text
    with sqlite3.connect(tmp_db) as conn:
        lease, error_info = conn.execute(
            "SELECT lease_expires_at, error_info FROM tasks WHERE task_id='orphan'"
        ).fetchone()
        assert lease is None
        assert json.loads(error_info)["reason"] == "orphan_lease_expired"
    assert queue.dequeue(role="implementer", claim_source="dispatcher").task_id == "next"


@pytest.mark.parametrize(("expiry", "heartbeat", "now"), [
    (5000.0, None, 4000.0),     # live lease
    (1000.0, 3000.0, 4000.0),   # worker heartbeated after expiry, still fresh (#887)
    (1000.0, None, 2500.0),     # within the default grace period
])
def test_live_lease_heartbeat_or_grace_prevents_expiry(
        tmp_db, monkeypatch, expiry, heartbeat, now):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    queue = TaskQueue(tmp_db)
    _claimed(queue, lease_expires_at=expiry, heartbeat_at=heartbeat)
    app = _app(tmp_db)

    with TestClient(app):
        actions = app.state.watchdog_tick(now=now)

    assert actions["timed_out"] == []
    assert queue.get_task_status("orphan") == "in_progress"


def test_live_dispatcher_owner_prevents_expiry(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    queue = TaskQueue(tmp_db)
    _claimed(queue)
    app = _app(tmp_db)
    app.state.dispatcher_active_tasks = {"orphan": object()}

    with TestClient(app):
        actions = app.state.watchdog_tick(now=4000.0)

    assert actions["timed_out"] == []
    assert queue.get_task_status("orphan") == "in_progress"


def test_grace_period_env_override(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setenv("AGENT_CREW_ORPHAN_GRACE_S", "60")
    queue = TaskQueue(tmp_db)
    _claimed(queue)
    app = _app(tmp_db)

    with TestClient(app):
        actions = app.state.watchdog_tick(now=1200.0)

    assert actions["timed_out"] == ["orphan"]
    assert queue.get_task_status("orphan") == "timed_out"


def test_busy_owned_pane_prevents_expiry(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    queue = TaskQueue(tmp_db)
    _claimed(queue)
    with sqlite3.connect(tmp_db) as conn:
        conn.execute("UPDATE tasks SET dispatch_target='%1' WHERE task_id='orphan'")
    app = _app(tmp_db, pane_map={"implementer": "%1"}, busy=True)

    with TestClient(app):
        actions = app.state.watchdog_tick(now=4000.0)

    assert actions["timed_out"] == []
    assert queue.get_task_status("orphan") == "in_progress"
