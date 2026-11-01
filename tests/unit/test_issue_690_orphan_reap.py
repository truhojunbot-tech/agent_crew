"""Dispatcher orphans expire without changing the tmux reminder grace."""

import json
import sqlite3
import time

from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _claimed(queue, task_id, *, source="dispatcher", dispatched_at=1000.0,
             heartbeat_at=None):
    queue.enqueue(TaskRequest(task_id=task_id, task_type="test",
                              description="run tests", branch="main"))
    with sqlite3.connect(queue._db_path) as db:
        db.execute("UPDATE tasks SET status='in_progress', claim_source=?, "
                   "last_activity_at=1000, dispatched_at=?, dispatch_agent='gemini', "
                   "dispatch_channel=?, dispatch_target=? WHERE task_id=?",
                   (source, dispatched_at,
                    "gemini_cli" if source == "dispatcher" else "tmux_pane",
                    "pid:123" if source == "dispatcher" else "%2915", task_id))
    if heartbeat_at is not None:
        queue.record_heartbeat(task_id, source="output_progress", ts=heartbeat_at)


def _app(db, *, busy, panes=None):
    return create_app(db, pane_map={"tester": "%2915"} if panes is None else panes,
                      pane_busy_fn=lambda _: busy,
                      pane_liveness_fn=lambda _: "alive",
                      push_fn=lambda *_: None,
                      timeout_seconds=900, reminder_seconds=300,
                      watchdog_disabled=True, anomaly_disabled=True)


def test_dispatcher_orphan_expires_without_reminder(tmp_db, monkeypatch, caplog):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    queue = TaskQueue(tmp_db)
    app = _app(tmp_db, busy=True)
    with TestClient(app):
        _claimed(queue, "orphan")
        actions = app.state.watchdog_tick(now=4000.0)
    assert actions["timed_out"] == ["orphan"]
    assert queue.get_task_status("orphan") == "timed_out"
    assert app.state.watchdog_expiry_counts["dispatcher_idle"] == 1
    assert "reason=dispatcher_idle" in caplog.text


def test_dispatcher_with_fresh_heartbeat_survives_alive_pane(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    queue = TaskQueue(tmp_db)
    app = _app(tmp_db, busy=True)
    with TestClient(app):
        _claimed(queue, "active", heartbeat_at=3990.0)
        actions = app.state.watchdog_tick(now=4000.0)
    assert actions["timed_out"] == []
    assert queue.get_task_status("active") == "in_progress"


def test_dispatcher_with_fresh_heartbeat_survives_without_pane(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    queue = TaskQueue(tmp_db)
    app = _app(tmp_db, busy=False, panes={})
    with TestClient(app):
        _claimed(queue, "active", heartbeat_at=3990.0)
        actions = app.state.watchdog_tick(now=4000.0)
    assert actions["timed_out"] == []
    assert queue.get_task_status("active") == "in_progress"


def test_dispatcher_with_stale_heartbeat_expires(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    queue = TaskQueue(tmp_db)
    app = _app(tmp_db, busy=True)
    with TestClient(app):
        _claimed(queue, "stale", heartbeat_at=1100.0)
        actions = app.state.watchdog_tick(now=4000.0)
    assert actions["timed_out"] == ["stale"]
    assert queue.get_task_status("stale") == "timed_out"


def test_later_dispatch_on_same_role_pane_expires_earlier_at_once(tmp_db, monkeypatch, caplog):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    queue = TaskQueue(tmp_db)
    app = _app(tmp_db, busy=True)
    with TestClient(app):
        _claimed(queue, "old")
        _claimed(queue, "new", dispatched_at=1100.0)
        actions = app.state.watchdog_tick(now=1200.0)
    assert actions["timed_out"] == ["old"]
    assert queue.get_task_status("old") == "timed_out"
    assert queue.get_task_status("new") == "in_progress"
    assert app.state.watchdog_expiry_counts["dispatcher_superseded"] == 1
    assert "reason=dispatcher_superseded" in caplog.text


def test_later_dispatch_on_another_pane_does_not_expire_earlier(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    queue = TaskQueue(tmp_db)
    app = _app(tmp_db, busy=True,
               panes={"tester": "%2915", "gemini": "%2999"})
    with TestClient(app):
        _claimed(queue, "old")
        _claimed(queue, "other-pane", dispatched_at=1100.0)
        with sqlite3.connect(tmp_db) as db:
            db.execute("UPDATE tasks SET context=? WHERE task_id='other-pane'",
                       (json.dumps({"agent_override": "gemini"}),))
        actions = app.state.watchdog_tick(now=1200.0)
    assert actions["timed_out"] == []
    assert queue.get_task_status("old") == "in_progress"


def test_background_watchdog_loop_expires_idle_dispatcher_claim(tmp_db, monkeypatch):
    """The periodic loop runs and stops with app lifespan in dispatcher mode."""
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    queue = TaskQueue(tmp_db)
    app = create_app(tmp_db, pane_map={}, timeout_seconds=0.05,
                     watchdog_interval=0.05, watchdog_disabled=False,
                     anomaly_disabled=True)
    with TestClient(app):
        _claimed(queue, "loop-idle")
        deadline = time.monotonic() + 2
        while queue.get_task_status("loop-idle") == "in_progress" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert queue.get_task_status("loop-idle") == "timed_out"
    assert app.state.watchdog_expiry_counts["dispatcher_idle"] == 1
