"""Stress test: 50 task enqueue/complete cycles with zero WATCHDOG TIMEOUT.

Acceptance criterion for #121: after N sequential enqueue→dequeue→complete
cycles through the HTTP API, the server log must contain zero WATCHDOG TIMEOUT
entries. No real agents or tmux panes are needed — the test drives the server
directly as an MCP-pull client would.
"""
import logging
import time

import pytest
from fastapi.testclient import TestClient

from agent_crew.server import create_app

pytestmark = pytest.mark.stress

TASK_COUNT = 50
ROLE = "coder"


@pytest.fixture
def stress_app(tmp_db):
    """Server with no pane_map (MCP-pull mode) and fast watchdog for quick ticks."""
    return create_app(
        tmp_db,
        pane_map=None,           # No tmux panes — agents pull via HTTP/MCP
        watchdog_disabled=False, # Keep watchdog enabled; it must NOT fire
        watchdog_interval=0.05,  # Fast tick so any spurious fire shows up quickly
        reminder_seconds=1.0,
        timeout_seconds=2.0,
        anomaly_disabled=True,
    )


@pytest.fixture
def stress_client(stress_app):
    with TestClient(stress_app) as client:
        yield client


# ---------------------------------------------------------------------------
# S-01: 50 sequential cycles — zero watchdog timeouts
# ---------------------------------------------------------------------------

def test_s01_50_runs_no_watchdog_timeout(stress_client, caplog):
    """50 enqueue→dequeue→complete cycles must produce zero WATCHDOG TIMEOUT logs."""
    with caplog.at_level(logging.ERROR, logger="agent_crew.server"):
        _run_50_cycles(stress_client)

    timeout_records = [
        r for r in caplog.records if "WATCHDOG TIMEOUT" in r.message
    ]
    assert len(timeout_records) == 0, (
        f"Expected 0 WATCHDOG TIMEOUT events, got {len(timeout_records)}:\n"
        + "\n".join(r.message for r in timeout_records)
    )


# ---------------------------------------------------------------------------
# S-02: all 50 tasks reach status=completed (no lost tasks)
# ---------------------------------------------------------------------------

def test_s02_all_tasks_complete(stress_client):
    """All 50 tasks must transition to completed — none stuck in pending/in_progress."""
    _run_50_cycles(stress_client)

    resp = stress_client.get("/tasks", params={"status": "completed"})
    assert resp.status_code == 200
    completed = resp.json()
    assert len(completed) == TASK_COUNT, (
        f"Expected {TASK_COUNT} completed tasks, got {len(completed)}"
    )


# ---------------------------------------------------------------------------
# S-03: manual watchdog tick after completion — still no timeouts
# ---------------------------------------------------------------------------

def test_s03_watchdog_tick_after_completion_clean(stress_client, stress_app):
    """After all tasks complete, a manual watchdog tick must report timed_out=[]."""
    _run_50_cycles(stress_client)

    # Drive the watchdog deterministically — no background asyncio needed.
    tick_fn = stress_app.state.watchdog_tick
    result = tick_fn(time.time() + 9999)  # Far-future now; would expire any in_progress task
    assert result["timed_out"] == [], (
        f"Watchdog tick found timed_out tasks after all completed: {result['timed_out']}"
    )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _run_50_cycles(client: TestClient) -> list[str]:
    """Enqueue TASK_COUNT tasks then drain them one by one. Returns completed task_ids."""
    # Enqueue all tasks first
    for i in range(TASK_COUNT):
        resp = client.post("/tasks", json={
            "task_id": f"stress-{i:03d}",
            "task_type": "implement",
            "description": f"Stress probe #{i} — complete immediately",
            "branch": "main",
            "priority": 3,
            "context": {"coordinator_managed": True},
        })
        assert resp.status_code == 201, f"enqueue {i} failed: {resp.text}"

    # Drain: dequeue → complete each task
    completed_ids: list[str] = []
    for _ in range(TASK_COUNT):
        resp = client.get("/tasks/next", params={"role": ROLE})
        assert resp.status_code == 200, f"GET /tasks/next failed: {resp.text}"
        task = resp.json()
        assert task is not None, "GET /tasks/next returned null before all tasks drained"

        task_id = task["task_id"]
        resp = client.post(f"/tasks/{task_id}/result", json={
            "task_id": task_id,
            "status": "completed",
            "summary": "stress probe done",
            "findings": [],
        })
        assert resp.status_code == 200, f"submit_result for {task_id} failed: {resp.text}"
        completed_ids.append(task_id)

    return completed_ids
