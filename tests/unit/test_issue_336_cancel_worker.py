"""#336: cancellation signals only a worker pane bound by dispatch."""

import json
import subprocess
from unittest.mock import patch

from fastapi.testclient import TestClient

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
        assert client.delete("/tasks/running").json()["worker_reachable"] is False
    assert response.json()["worker_reachable"] is True
    assert [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["tmux", "send-keys"]] == [
        ["tmux", "send-keys", "-t", "%101", "C-c"]]
    assert _events(tmp_db)[-1]["outcome"] == "sent"


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
        for task_id in ("pending", "terminal"):
            assert client.delete(f"/tasks/{task_id}").json()["worker_reachable"] is False
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
