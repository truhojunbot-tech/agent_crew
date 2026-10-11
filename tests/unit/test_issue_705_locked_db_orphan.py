"""A failed dispatcher write must leave a recoverable claim."""

import asyncio
import json
import sqlite3
import threading
import time

import click
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


def test_watchdog_keeps_young_unstarted_claim(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    queue = TaskQueue(tmp_db)
    _claimed(queue, "young", age_seconds=120)
    app = create_app(tmp_db, pane_map={}, worktree_map={},
                     watchdog_disabled=True, anomaly_disabled=True,
                     timeout_seconds=10000)
    with TestClient(app):
        actions = app.state.watchdog_tick(time.time())
    assert actions.get("recovered") is None
    assert queue.get_task_status("young") == "in_progress"


def test_watchdog_keeps_active_unstarted_claim(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    queue = TaskQueue(tmp_db)
    _claimed(queue, "active", age_seconds=1000)
    app = create_app(tmp_db, pane_map={}, worktree_map={},
                     watchdog_disabled=True, anomaly_disabled=True,
                     timeout_seconds=10000)
    with TestClient(app):
        app.state.dispatcher_active_tasks = {"active": object()}
        actions = app.state.watchdog_tick(time.time())
    assert actions.get("recovered") is None
    assert queue.get_task_status("active") == "in_progress"


@pytest.mark.parametrize("interval,expected_recovered", [
    ("60", True), ("invalid", False), ("0", False),
])
def test_watchdog_unstarted_recovery_interval_override(
        tmp_db, monkeypatch, interval, expected_recovered):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_CLAIM_RECOVER_SECONDS", interval)
    queue = TaskQueue(tmp_db)
    _claimed(queue, "interval", age_seconds=120)
    app = create_app(tmp_db, pane_map={}, worktree_map={},
                     watchdog_disabled=True, anomaly_disabled=True,
                     timeout_seconds=10000)
    with TestClient(app):
        actions = app.state.watchdog_tick(time.time())
    assert actions.get("recovered") == (["interval"] if expected_recovered else None)
    assert queue.get_task_status("interval") == (
        "pending" if expected_recovered else "in_progress")


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


def test_crew_run_locked_read_exhaustion_and_other_sqlite_error(monkeypatch):
    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)
    calls = []

    def locked(task_id):
        calls.append(task_id)
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(click.ClickException, match="database remained locked"):
        cli_module._retry_locked_queue_read(locked, "task")
    assert calls == ["task"] * 4

    def other(task_id):
        raise sqlite3.OperationalError("no such table: tasks")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        cli_module._retry_locked_queue_read(other, "task")


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


def test_dispatcher_run_boundary_fails_unexpected_pre_spawn_error_and_proceeds(
        tmp_db, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", "0.05")
    monkeypatch.setattr(server_module, "_WORKTREE_SYNC_DISABLED", True)
    monkeypatch.setattr(server_module, "_ensure_role_protocol", lambda *a, **k: True)
    calls = []

    def unexpected(_queue, task_id, *args, **kwargs):
        calls.append(task_id)
        raise RuntimeError("injected pre-spawn failure")

    monkeypatch.setattr(TaskQueue, "record_dispatch", unexpected)
    queue = TaskQueue(tmp_db)
    app = create_app(tmp_db, pane_map={},
                     worktree_map={"implementer": str(tmp_path)},
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        for task_id in ("unexpected", "next"):
            queue.enqueue(TaskRequest(task_id=task_id, task_type="implement",
                                      description="work", branch="main"))
        deadline = time.monotonic() + 3
        while (queue.get_task_status("unexpected") != "failed" or
               queue.get_task_status("next") != "failed") and time.monotonic() < deadline:
            time.sleep(0.02)
    assert calls == ["unexpected", "next"]
    assert queue.get_task_status("unexpected") == "failed"
    assert queue.get_task_status("next") == "failed"
    assert "dispatcher: task unexpected raised" in caplog.text
    assert "Task exception was never retrieved" not in caplog.text


def test_reclaimed_active_dispatch_is_not_reaped_as_superseded(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    queue = TaskQueue(tmp_db)
    app = create_app(tmp_db, pane_map={"implementer": "%2915"},
                     pane_busy_fn=lambda _: True, pane_liveness_fn=lambda _: "alive",
                     watchdog_disabled=True, anomaly_disabled=True,
                     timeout_seconds=900)
    with TestClient(app):
        for task_id in ("A", "B"):
            queue.enqueue(TaskRequest(task_id=task_id, task_type="implement",
                                      description=f"work {task_id}", branch="main"))
        assert queue.dequeue(role="implementer", agent="codex", claimed_via="dispatcher",
                             claim_source="dispatcher").task_id == "A"
        queue.record_dispatch("A", channel="codex_exec", agent="codex", target="pid:100")
        assert queue.requeue_dispatcher_claim("A")
        assert queue.dequeue(role="implementer", agent="codex", claimed_via="dispatcher",
                             claim_source="dispatcher", skip_task_ids={"A"}).task_id == "B"
        queue.record_dispatch("B", channel="codex_exec", agent="codex", target="pid:101")
        assert queue.dequeue(role="implementer", agent="codex", claimed_via="dispatcher",
                             claim_source="dispatcher").task_id == "A"
        app.state.dispatcher_active_tasks = {"A": object()}
        actions = app.state.watchdog_tick(time.time())
    assert actions["timed_out"] == []
    assert queue.get_task_status("A") == "in_progress"


def test_dispatcher_run_boundary_requeues_locked_pre_spawn_error(tmp_db, tmp_path,
                                                                 monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", "0.2")
    monkeypatch.setattr(server_module, "codex_session_for_cwd",
                        lambda *_: (_ for _ in ()).throw(
                            sqlite3.OperationalError("database is locked")))
    recovered = threading.Event()
    real_requeue = TaskQueue.requeue_dispatcher_claim

    def requeue(self, task_id, **kwargs):
        result = real_requeue(self, task_id, **kwargs)
        if result and task_id == "boundary-locked":
            recovered.set()
        return result

    monkeypatch.setattr(TaskQueue, "requeue_dispatcher_claim", requeue)
    queue = TaskQueue(tmp_db)
    app = create_app(tmp_db, pane_map={},
                     worktree_map={"implementer": str(tmp_path)},
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        queue.enqueue(TaskRequest(task_id="boundary-locked", task_type="implement",
                                  description="work", branch="main"))
        assert recovered.wait(3), "the _run exception boundary did not requeue"
        assert queue.get_task_status("boundary-locked") == "pending"


@pytest.mark.parametrize("failure", ["locked", "refused"])
def test_requeue_failure_is_recovered_by_watchdog_backstop(tmp_db, tmp_path,
                                                          monkeypatch, failure):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setattr(server_module, "_WORKTREE_SYNC_DISABLED", True)
    monkeypatch.setattr(server_module, "_ensure_role_protocol", lambda *a, **k: True)
    monkeypatch.setattr(server_module, "codex_session_for_cwd", lambda *a: "")
    monkeypatch.setattr(server_module, "codex_context_exceeds_cap", lambda *a, **k: (False, {}))
    monkeypatch.setattr(TaskQueue, "record_dispatch",
                        lambda *a, **k: (_ for _ in ()).throw(
                            sqlite3.OperationalError("database is locked")))
    real_requeue = TaskQueue.requeue_dispatcher_claim
    calls = 0

    def refused_requeue(self, task_id, **kwargs):
        nonlocal calls
        calls += 1
        if calls <= (3 if failure == "locked" else 1):
            if failure == "locked":
                raise sqlite3.OperationalError("database is locked")
            return False
        return real_requeue(self, task_id, **kwargs)

    monkeypatch.setattr(TaskQueue, "requeue_dispatcher_claim", refused_requeue)
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id=f"backstop-{failure}", task_type="implement",
                              description="work", branch="main"))
    claim = queue.dequeue(role="implementer", agent="codex",
                          claimed_via="dispatcher", claim_source="dispatcher")
    app = create_app(tmp_db, pane_map={},
                     worktree_map={"implementer": str(tmp_path)},
                     watchdog_disabled=True, anomaly_disabled=True,
                     timeout_seconds=10000)
    with TestClient(app):
        asyncio.run(app.state.dispatch_task(claim, "implementer"))
        assert queue.get_task_status(claim.task_id) == "in_progress"
        with sqlite3.connect(tmp_db) as db:
            db.execute("UPDATE tasks SET last_activity_at=?,dispatched_at=NULL "
                       "WHERE task_id=?", (time.time() - 1000, claim.task_id))
            db.commit()
        actions = app.state.watchdog_tick(time.time())
    assert actions["recovered"] == [claim.task_id]
    assert queue.get_task_status(claim.task_id) == "pending"
