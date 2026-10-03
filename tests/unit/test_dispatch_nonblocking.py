"""Dispatcher git preparation must not stall unrelated HTTP responses."""

import threading
import time

from fastapi.testclient import TestClient

import agent_crew.server as server
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def test_slow_worktree_prep_does_not_block_health(tmp_path, monkeypatch):
    entered = threading.Event()
    worktree = tmp_path / "worker"
    worktree.mkdir()
    (tmp_path / "port").write_text("19991\n")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", "0.01")
    monkeypatch.delenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", raising=False)

    def slow_prep(*args, **kwargs):
        entered.set()
        time.sleep(2)
        raise server.WorktreePrepRefused("test prep stopped")

    monkeypatch.setattr(server, "_prepare_worktree_for_task", slow_prep)
    db_path = str(tmp_path / "tasks.db")
    TaskQueue(db_path).enqueue(TaskRequest(
        task_id="impl-slow-prep", task_type="implement",
        description="exercise dispatcher", branch="main", project="test_project",
        context={},
    ))
    app = create_app(
        db_path, pane_map={}, port=19991,
        worktree_map={"implementer": str(worktree)},
        watchdog_disabled=True, anomaly_disabled=True,
    )
    with TestClient(app) as client:
        assert entered.wait(3), "dispatcher did not enter worktree preparation"
        started = time.monotonic()
        health = client.get("/health")
        elapsed = time.monotonic() - started
    assert health.status_code == 200
    assert elapsed < 0.5, f"health waited {elapsed:.2f}s for dispatcher git prep"
