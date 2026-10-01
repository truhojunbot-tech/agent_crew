"""#336: cancellation signals only a worker pane bound by dispatch."""

import json
import subprocess
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from click.testing import CliRunner

from agent_crew.cli import crew
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _app(tmp_db, pane_map=None, state_path=None):
    with patch.dict("os.environ", {"AGENT_CREW_DISPATCHER": "0"}):
        return create_app(tmp_db, pane_map=pane_map, state_path=state_path, port=8100,
                          push_fn=lambda *_: None, watchdog_disabled=True,
                          anomaly_disabled=True)


def _events(tmp_db):
    from pathlib import Path
    path = Path(tmp_db).with_name("context_events.jsonl")
    return [json.loads(line) for line in path.read_text().splitlines()
            if json.loads(line)["event_type"] == "cancel_signalled"]


def test_cancel_in_progress_interrupts_bound_pane_once(tmp_db):
    app = _app(tmp_db, {"implementer": "%101"})
    with patch("agent_crew.server.subprocess.run") as run, TestClient(app) as client:
        run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        created = client.post("/tasks", json={"task_id": "running", "task_type": "implement",
                                              "description": "work", "branch": "main"})
        assert created.status_code == 201
        response = client.delete("/tasks/running")
        # G12 (unified): a second DELETE on the now-terminal row is refused as a
        # no-op 409 NOT_ACTIVE and signals nothing.
        again = client.delete("/tasks/running")
        assert again.status_code == 409 and again.json()["reason"] == "NOT_ACTIVE"
    assert response.json()["worker_reachable"] is True
    assert [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["tmux", "send-keys"]] == [
        ["tmux", "send-keys", "-t", "%101", "C-c"]]
    assert response.json()["cancel_signal_outcome"] == "ack_timeout"
    assert response.json()["pane_exit_observed"] is False
    assert _events(tmp_db)[-1]["outcome"] == "ack_timeout"


def test_cancel_in_progress_without_bound_pane_records_unreachable(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id="pulled", task_type="implement", description="work"))
    queue.dequeue(role="implementer")
    app = _app(tmp_db, {"implementer": "%101"})
    with patch("agent_crew.server.subprocess.run") as run, TestClient(app) as client:
        response = client.delete("/tasks/pulled")
    assert response.json()["worker_reachable"] is False
    assert not any(c.args[0][:2] == ["tmux", "send-keys"] for c in run.call_args_list)
    assert _events(tmp_db)[-1]["outcome"] == "unreachable"


def test_pending_and_terminal_cancel_do_not_interrupt(tmp_db):
    queue = TaskQueue(tmp_db)
    for task_id in ("terminal", "pending"):
        queue.enqueue(TaskRequest(task_id=task_id, task_type="implement", description="work"))
    queue.dequeue(role="implementer")
    queue.force_fail("terminal", "fixture")
    app = _app(tmp_db, {"implementer": "%101"})
    with patch("agent_crew.server.subprocess.run") as run, TestClient(app) as client:
        assert client.delete("/tasks/pending").json()["worker_reachable"] is False
        # G12 (unified): a terminal row is refused (409 NOT_ACTIVE), not re-cancelled.
        terminal = client.delete("/tasks/terminal")
        assert terminal.status_code == 409
        assert terminal.json() == {"status": "failed", "reason": "NOT_ACTIVE",
                                   "task_id": "terminal"}
    assert not any(c.args[0][:2] == ["tmux", "send-keys"] for c in run.call_args_list)


def test_cancel_refuses_pane_outside_project_ownership(tmp_db, tmp_path):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id="foreign", task_type="implement", description="work"))
    queue.dequeue(role="implementer")
    queue.set_push_at("foreign", pane_id="%999")
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"pane_ids": ["%101"]}))
    app = _app(tmp_db, {"implementer": "%101"}, str(state_path))
    with patch("agent_crew.server.subprocess.run") as run, TestClient(app) as client:
        response = client.delete("/tasks/foreign")
    assert response.json()["worker_reachable"] is False
    assert not any(c.args[0][:2] == ["tmux", "send-keys"] for c in run.call_args_list)
    assert _events(tmp_db)[-1]["outcome"] == "unreachable"


def test_cancel_observes_pane_exit_after_signal(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id="exiting", task_type="implement", description="work"))
    queue.dequeue(role="implementer")
    queue.set_push_at("exiting", pane_id="%101")
    app = _app(tmp_db, {"implementer": "%101"})
    with patch("agent_crew.server._pane_alive_for_push", side_effect=[True, False]), \
         patch("agent_crew.server.subprocess.run") as run, TestClient(app) as client:
        run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        response = client.delete("/tasks/exiting")
    assert response.json()["cancel_signal_outcome"] == "pane_exited"
    assert response.json()["pane_exit_observed"] is True


@pytest.mark.parametrize("signal_returncode, outcome", [
    (0, "ack_timeout"), (1, "send_failed"),
])
def test_cli_cancel_signals_bound_worker_and_reports_unconfirmed_stop(
        tmp_db, tmp_path, signal_returncode, outcome):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id="cli-running", task_type="implement", description="work"))
    queue.dequeue(role="implementer")
    queue.set_push_at("cli-running", pane_id="%101")
    (tmp_path / "state.json").write_text(json.dumps({"pane_ids": ["%101"]}))
    with patch("agent_crew.server.subprocess.run") as run:
        run.side_effect = lambda args, **_kw: subprocess.CompletedProcess(
            args, signal_returncode if args[1] == "send-keys" else 0,
            stdout="", stderr="",
        )
        result = CliRunner().invoke(crew, ["task", "cancel", "cli-running", "--db", tmp_db])
    assert any(c.args[0] == ["tmux", "send-keys", "-t", "%101", "C-c"]
               for c in run.call_args_list)
    assert result.exit_code != 0
    assert "unconfirmed" in result.output.lower()
    assert outcome in result.output


def test_cli_project_cancel_signals_its_bound_worker(tmp_path):
    project_dir = tmp_path / "demo"
    project_dir.mkdir()
    db = str(project_dir / "tasks.db")
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest(task_id="project-running", task_type="implement",
                              description="work"))
    queue.dequeue(role="implementer")
    queue.set_push_at("project-running", pane_id="%101")
    (project_dir / "state.json").write_text(json.dumps({
        "db": db, "port": 8100, "pane_ids": ["%101"],
    }))
    with patch("agent_crew.server.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        result = CliRunner().invoke(crew, ["task", "cancel", "project-running",
                                             "--project", "demo", "--base", str(tmp_path)])
    assert any(c.args[0] == ["tmux", "send-keys", "-t", "%101", "C-c"]
               for c in run.call_args_list)
    assert result.exit_code != 0
    assert "unconfirmed" in result.output.lower()


def test_result_after_cancel_is_rejected_without_cascade(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id="late", task_type="implement", description="work"))
    queue.dequeue(role="implementer")
    app = _app(tmp_db)
    with TestClient(app) as client:
        assert client.delete("/tasks/late").status_code == 200
        response = client.post("/tasks/late/result", json={
            "task_id": "late", "status": "completed", "summary": "worker kept running",
        })
    assert response.status_code == 409
    # Unified with G12: the one guard answers with its structured body.
    assert response.json()["late_result"] is True
    assert response.json()["accepted"] is False
    assert response.json()["prior_status"] == "cancelled"
    assert queue.get_task_status("late") == "cancelled"
    assert queue.outbox_get("late") is None
