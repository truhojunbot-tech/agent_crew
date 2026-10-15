"""Human-waiting tasks can be closed through the supported cancel paths."""

import json
import sqlite3

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from agent_crew.cli import crew
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _task_at_status(db, task_id, status):
    queue = TaskQueue(str(db))
    queue.enqueue(TaskRequest(task_id=task_id, task_type="implement",
                              description="waiting for human", branch="main"))
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE tasks SET status=? WHERE task_id=?", (status, task_id))
    return queue


def _cancel_audit(db, task_id):
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT fields FROM task_exec_events WHERE task_id=? AND event='cancelled' "
            "ORDER BY event_id DESC LIMIT 1", (task_id,)).fetchone()
    return json.loads(row[0]) if row else None


@pytest.mark.parametrize("prior", ["blocked", "needs_human"])
def test_http_cancels_human_waiting_with_prior_status_and_reason(tmp_path, monkeypatch, prior):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    db = tmp_path / "tasks.db"
    task_id = f"waiting-{prior}"
    queue = _task_at_status(db, task_id, prior)
    app = create_app(str(db), watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.delete(f"/tasks/{task_id}?reason=obsolete_pr")
    assert response.status_code == 200, response.text
    assert queue.get_task_status(task_id) == "cancelled"
    assert _cancel_audit(db, task_id) == {
        "prior_status": prior, "reason": "obsolete_pr",
    }


@pytest.mark.parametrize("prior", ["blocked", "needs_human"])
def test_cli_cancels_human_waiting_with_reason(tmp_path, monkeypatch, prior):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    db = tmp_path / "tasks.db"
    task_id = f"cli-{prior}"
    queue = _task_at_status(db, task_id, prior)
    result = CliRunner().invoke(
        crew, ["task", "cancel", task_id, "--db", str(db),
               "--reason", "obsolete_pr"])
    assert result.exit_code == 0, result.output
    assert queue.get_task_status(task_id) == "cancelled"
    assert _cancel_audit(db, task_id) == {
        "prior_status": prior, "reason": "obsolete_pr",
    }


@pytest.mark.parametrize("prior", ["completed", "failed", "cancelled", "timed_out"])
def test_terminal_cancel_still_returns_not_active(tmp_path, monkeypatch, prior):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    db = tmp_path / "tasks.db"
    task_id = f"terminal-{prior}"
    queue = _task_at_status(db, task_id, prior)
    app = create_app(str(db), watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.delete(f"/tasks/{task_id}?reason=obsolete_pr")
    assert response.status_code == 409, response.text
    assert response.json()["reason"] == "NOT_ACTIVE"
    assert queue.get_task_status(task_id) == prior
    assert _cancel_audit(db, task_id) is None
