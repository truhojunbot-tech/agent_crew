"""#362: tmux push refuses ghost tasks and foreign panes."""

import json

import pytest
from fastapi.testclient import TestClient

from agent_crew.queue import TaskQueue
from agent_crew.protocol import TaskRequest
from agent_crew.server import create_app


class RecordingPush:
    def __init__(self):
        self.calls = []

    def __call__(self, target, message):
        self.calls.append((target, message))


@pytest.fixture
def project_state(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"project": "owned", "pane_ids": ["%101"]}))
    return path


def _payload(task_id="guarded"):
    return {
        "task_id": task_id, "task_type": "implement", "description": "work",
        "branch": "main", "priority": 3, "context": {}, "project": "owned",
    }


@pytest.mark.parametrize("status", [None, "failed"])
def test_push_refuses_missing_or_terminal_task(tmp_db, project_state, caplog, status):
    push = RecordingPush()
    app = create_app(tmp_db, pane_map={"implementer": "%101"}, state_path=str(project_state),
                     port=8100, push_fn=push, watchdog_disabled=True, anomaly_disabled=True)
    task_id = "ghost" if status is None else "terminal"
    if status is not None:
        queue = TaskQueue(tmp_db)
        queue.enqueue(TaskRequest(task_id=task_id, task_type="implement", description="done"))
        queue.dequeue(role="implementer")
        queue.force_fail(task_id, "terminal fixture")

    with TestClient(app) as client:
        result = client.app.state.guard_tmx_push(task_id, "%101")

    assert result == ""
    assert push.calls == []
    assert "refusing tmux dispatch" in caplog.text
    expected_reason = "task_missing" if status is None else "task_status_failed"
    assert expected_reason in caplog.text


def test_push_refuses_pane_not_owned_by_project(tmp_db, project_state, caplog):
    push = RecordingPush()
    app = create_app(tmp_db, pane_map={"implementer": "%999"}, state_path=str(project_state),
                     port=8100, push_fn=push, watchdog_disabled=True, anomaly_disabled=True)

    with TestClient(app) as client:
        response = client.post("/tasks", json=_payload())

    assert response.status_code == 201
    assert push.calls == []
    assert "pane_not_owned" in caplog.text
    assert next(row["status"] for row in TaskQueue(tmp_db).list_all_with_status()
                if row["task_id"] == "guarded") == "failed"


def test_push_resolves_named_target_before_ownership_check(tmp_db, project_state, monkeypatch, caplog):
    push = RecordingPush()
    resolved_targets = []

    def resolve(target):
        resolved_targets.append(target)
        return "%999"

    monkeypatch.setattr("agent_crew.server._resolve_tmux_pane_target", resolve)
    app = create_app(tmp_db, pane_map={"implementer": "foreign:0.1"}, state_path=str(project_state),
                     port=8100, push_fn=push, watchdog_disabled=True, anomaly_disabled=True)

    with TestClient(app) as client:
        response = client.post("/tasks", json=_payload())

    assert response.status_code == 201
    assert resolved_targets == ["foreign:0.1"]
    assert push.calls == []
    assert "resolved=%999 reason=pane_not_owned" in caplog.text


def test_push_refuses_owned_pane_that_no_longer_exists(tmp_db, project_state, monkeypatch, caplog):
    push = RecordingPush()
    monkeypatch.setattr("agent_crew.server._pane_alive_for_push", lambda _target: False)
    app = create_app(tmp_db, pane_map={"implementer": "%101"}, state_path=str(project_state),
                     port=8100, push_fn=push, watchdog_disabled=True, anomaly_disabled=True)

    with TestClient(app) as client:
        response = client.post("/tasks", json=_payload())

    assert response.status_code == 201
    assert push.calls == []
    assert "pane_dead" in caplog.text


def test_push_delivers_existing_task_to_live_owned_pane(tmp_db, project_state):
    push = RecordingPush()
    app = create_app(tmp_db, pane_map={"implementer": "%101"}, state_path=str(project_state),
                     port=8100, push_fn=push, watchdog_disabled=True, anomaly_disabled=True)

    with TestClient(app) as client:
        response = client.post("/tasks", json=_payload())

    assert response.status_code == 201
    assert len(push.calls) == 1
    assert push.calls[0][0] == "%101"


def test_guard_reads_current_pane_ids_after_state_changes(tmp_db, project_state, monkeypatch):
    monkeypatch.setattr("agent_crew.server._pane_alive_for_push", lambda _pane: True)
    app = create_app(tmp_db, pane_map={"implementer": "%101"}, state_path=str(project_state),
                     port=8100, push_fn=RecordingPush(), watchdog_disabled=True, anomaly_disabled=True)
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id="rotated", task_type="implement", description="work"))
    project_state.write_text(json.dumps({"project": "owned", "pane_ids": ["%202"]}))
    with TestClient(app) as client:
        result = client.app.state.guard_tmx_push("rotated", "%202")
    assert result == "%202"


def test_named_target_uses_tmux_canonical_pane_id(monkeypatch):
    from types import SimpleNamespace
    from agent_crew.server import _resolve_tmux_pane_target
    monkeypatch.setattr("agent_crew.server.subprocess.run",
                        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="%4242\n"))
    assert _resolve_tmux_pane_target("owned:0.1") == "%4242"
