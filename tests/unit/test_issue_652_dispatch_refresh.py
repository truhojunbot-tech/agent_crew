"""A slow policy refresh must not stop the dispatcher (#652)."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

import agent_crew.server as server
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


@pytest.mark.parametrize("blocked_refresh", ["rounds_cap", "owner_conflict"])
def test_hung_refresh_does_not_hold_dispatch_ticks(tmp_path, monkeypatch, blocked_refresh):
    entered = threading.Event()
    release = threading.Event()
    dispatched = threading.Event()
    calls = []

    def hang(*args, **kwargs):
        calls.append(1)
        entered.set()
        release.wait(10)

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", "0.01")
    monkeypatch.setattr(server, "_pipeline_reresolve_pending_rounds_caps",
                        hang if blocked_refresh == "rounds_cap" else lambda *a, **k: None)
    monkeypatch.setattr(TaskQueue, "readmit_parked_owner_conflicts",
                        hang if blocked_refresh == "owner_conflict" else lambda self: [])

    def prep(*args, **kwargs):
        dispatched.set()
        raise server.WorktreePrepRefused("test dispatch observed")

    monkeypatch.setattr(server, "_prepare_worktree_for_task", prep)
    worktree = tmp_path / "worker"
    worktree.mkdir()
    db_path = str(tmp_path / "tasks.db")
    TaskQueue(db_path).enqueue(TaskRequest(
        task_id="impl-after-refresh", task_type="implement",
        description="dispatch despite refresh", branch="main", project="test_project",
        context={"risk_tier": 1},
    ))
    app = create_app(db_path, pane_map={}, worktree_map={"implementer": str(worktree)},
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        try:
            assert entered.wait(3), "refresh did not start"
            assert dispatched.wait(3), "pending task was not dispatched while refresh hung"
            first = app.state.dispatcher_last_tick_monotonic
            deadline = time.monotonic() + 3
            while app.state.dispatcher_last_tick_monotonic == first and time.monotonic() < deadline:
                time.sleep(0.01)
            assert app.state.dispatcher_last_tick_monotonic > first
            assert client.get("/health").json()["dispatcher_tick_age_s"] < 1
            assert len(calls) == 1, "hung refresh started a second worker"
        finally:
            release.set()


def test_health_reports_dispatcher_tick_age(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    app = create_app(str(tmp_path / "tasks.db"), pane_map={},
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        assert client.get("/health").json()["dispatcher_tick_age_s"] is None
        app.state.dispatcher_last_tick_monotonic = time.monotonic() - 5
        age = client.get("/health").json()["dispatcher_tick_age_s"]
        assert 4.5 < age < 6
