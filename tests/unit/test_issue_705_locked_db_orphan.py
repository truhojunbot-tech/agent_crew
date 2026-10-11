"""A failed dispatcher write must leave a recoverable claim."""

import asyncio
import json
import sqlite3
import time

from fastapi.testclient import TestClient
import pytest

import agent_crew.server as server_module
import agent_crew.cli as cli_module
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _claimed(queue, task_id, *, age_seconds, dispatched=False):
    queue.enqueue(TaskRequest(task_id=task_id, task_type="implement",
                              description="work", branch="main"))
    claimed_at = time.time() - age_seconds
    with sqlite3.connect(queue._db_path) as db:
        db.execute(
            "UPDATE tasks SET status='in_progress', claim_source='dispatcher', "
            "last_activity_at=?, dispatched_at=?, lease_owner=? WHERE task_id=?",
            (claimed_at, claimed_at if dispatched else None,
             "codex:pid:123" if dispatched else None, task_id))


def test_dispatcher_mode_starts_watchdog(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", "60")
    queue = TaskQueue(tmp_db)
    app = create_app(tmp_db, pane_map={}, worktree_map={},
                     watchdog_interval=0.02, timeout_seconds=1,
                     anomaly_disabled=True)
    with TestClient(app):
        _claimed(queue, "expired", age_seconds=60, dispatched=True)
        deadline = time.monotonic() + 1
        while queue.get_task_status("expired") == "in_progress" and time.monotonic() < deadline:
            time.sleep(0.02)
    assert queue.get_task_status("expired") == "timed_out"
    assert app.state.watchdog_expiry_counts["dispatcher_idle"] == 1


def test_watchdog_recovers_unstarted_dispatcher_claim(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    queue = TaskQueue(tmp_db)
    _claimed(queue, "unstarted", age_seconds=1000)
    app = create_app(tmp_db, pane_map={}, worktree_map={},
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        actions = app.state.watchdog_tick(time.time())
    assert actions["recovered"] == ["unstarted"]
    assert queue.get_task_status("unstarted") == "pending"
    assert app.state.watchdog_expiry_counts["dispatcher_unstarted"] == 1


def test_watchdog_keeps_claim_with_live_dispatch_lease(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    queue = TaskQueue(tmp_db)
    _claimed(queue, "running", age_seconds=1000, dispatched=True)
    queue.record_heartbeat("running", source="process_alive")
    app = create_app(tmp_db, pane_map={}, worktree_map={},
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        actions = app.state.watchdog_tick(time.time())
    assert actions.get("recovered") is None
    assert queue.get_task_status("running") == "in_progress"


def test_watchdog_keeps_heartbeat_without_dispatch_lease(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    queue = TaskQueue(tmp_db)
    _claimed(queue, "heartbeat", age_seconds=1000)
    queue.record_heartbeat("heartbeat", source="process_alive")
    app = create_app(tmp_db, pane_map={}, worktree_map={},
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        actions = app.state.watchdog_tick(time.time())
    assert actions.get("recovered") is None
    assert queue.get_task_status("heartbeat") == "in_progress"


def test_queue_connection_has_busy_timeout(tmp_db):
    queue = TaskQueue(tmp_db)
    with queue._connect() as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 10000


def test_record_dispatch_propagates_locked_write(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id="locked-dispatch", task_type="implement",
                              description="work", branch="main"))
    queue.dequeue(role="implementer", claimed_via="dispatcher",
                  claim_source="dispatcher")

    calls = 0

    def locked(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(TaskQueue, "_append_exec_event_on", staticmethod(locked))
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        queue.record_dispatch("locked-dispatch", channel="codex_exec",
                              lease_owner="codex:pending", raise_on_locked=True)
    assert calls == 1
    assert queue.get_task_status("locked-dispatch") == "in_progress"


def test_http_poll_hands_out_claim_when_dispatch_record_is_locked(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DELIVERY", "push")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id="http-locked", task_type="implement",
                              description="work", branch="main"))

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(TaskQueue, "_append_exec_event_on", staticmethod(locked))
    app = create_app(tmp_db, project="agent_crew", pane_map={}, worktree_map={},
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/tasks/next?role=implementer", headers={
            "X-Agent-Crew-Project": "agent_crew"})

    assert response.status_code == 200
    assert response.json()["task_id"] == "http-locked"
    assert queue.get_task_status("http-locked") == "in_progress"


def test_crew_run_retries_locked_result_and_status_reads(monkeypatch):
    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)
    for value in ("result", "in_progress"):
        calls = 0

        def read(task_id):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise sqlite3.OperationalError("database is locked")
            return value

        assert cli_module._retry_locked_queue_read(read, "task") == value
        assert calls == 3


@pytest.mark.parametrize("write", ["record_dispatch", "patch_context"])
def test_locked_write_after_claim_requeues_without_spawning(
        tmp_db, tmp_path, monkeypatch, write):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setattr(server_module, "_WORKTREE_SYNC_DISABLED", True)
    monkeypatch.setattr(server_module, "_ensure_role_protocol", lambda *a, **k: True)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"codex_session_mode": "renew_rehydrate"}))
    monkeypatch.setattr(server_module, "codex_session_for_cwd", lambda *a: "previous")
    monkeypatch.setattr(server_module, "codex_latest_compaction_summary", lambda *a: "")
    monkeypatch.setattr(server_module, "codex_context_exceeds_cap", lambda *a, **k: (False, {}))

    calls = 0

    def locked(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(TaskQueue, write, locked)
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id="locked", task_type="implement",
                              description="work", branch="main"))
    claim = queue.dequeue(role="implementer", agent="codex",
                          claimed_via="dispatcher", claim_source="dispatcher")
    assert claim is not None
    app = create_app(tmp_db, pane_map={},
                     worktree_map={"implementer": str(tmp_path)},
                     state_path=str(state_path), watchdog_disabled=True,
                     anomaly_disabled=True)
    with TestClient(app):
        asyncio.run(app.state.dispatch_task(claim, "implementer"))
    assert calls >= 3
    assert queue.get_task_status("locked") == "pending"


def test_dispatcher_run_boundary_recovers_unexpected_pre_spawn_error(
        tmp_db, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", "1")
    monkeypatch.setattr(server_module, "_WORKTREE_SYNC_DISABLED", True)
    monkeypatch.setattr(server_module, "_ensure_role_protocol", lambda *a, **k: True)
    calls = 0

    def unexpected(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("injected pre-spawn failure")

    monkeypatch.setattr(TaskQueue, "record_dispatch", unexpected)
    queue = TaskQueue(tmp_db)
    app = create_app(tmp_db, pane_map={},
                     worktree_map={"implementer": str(tmp_path)},
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        queue.enqueue(TaskRequest(task_id="unexpected", task_type="implement",
                                  description="work", branch="main"))
        deadline = time.monotonic() + 3
        while (calls == 0 or queue.get_task_status("unexpected") != "pending") and time.monotonic() < deadline:
            time.sleep(0.02)
    assert calls >= 1
    assert queue.get_task_status("unexpected") == "pending"
    assert "dispatcher: task unexpected raised" in caplog.text
    assert "Task exception was never retrieved" not in caplog.text
