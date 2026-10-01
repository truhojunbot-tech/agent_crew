"""A recycled local port must not attach workers from another crew (#362)."""

import json
import subprocess
from unittest.mock import MagicMock, patch
from io import BytesIO

from click.testing import CliRunner
from fastapi.testclient import TestClient

from agent_crew.cli import crew
from agent_crew.instructions import generate
from agent_crew.project_identity import ProjectIdentityError, verify_server_identity
from agent_crew.server import _format_task_message, create_app
from agent_crew.queue import TaskRequest
from agent_crew.loop import _post_task_http


def test_health_keeps_existing_project_identity(tmp_path):
    with TestClient(create_app(str(tmp_path / "tasks.db"), project="alpha",
                               watchdog_disabled=True, anomaly_disabled=True)) as client:
        assert client.get("/health").json()["project"] == "alpha"


def test_worker_transport_refuses_missing_and_foreign_identity_before_claim(tmp_path):
    with TestClient(create_app(str(tmp_path / "tasks.db"), project="alpha",
                               identity_required=True, watchdog_disabled=True,
                               anomaly_disabled=True)) as client:
        payload = {"task_id": "impl-identity", "task_type": "implement",
                   "description": "work", "project": "alpha"}
        assert client.post("/tasks", json=payload, headers={
            "X-Agent-Crew-Project": "alpha"}).status_code == 201
        result = {"task_id": "impl-identity", "status": "completed", "summary": "done"}
        assert client.get("/tasks/next?role=implementer").status_code == 428
        assert client.get("/tasks/next?role=implementer", headers={
            "X-Agent-Crew-Project": "beta"}).status_code == 409
        assert client.post("/tasks/impl-identity/result", json=result).status_code == 428
        assert client.post("/tasks/impl-identity/result", json=result, headers={
            "X-Agent-Crew-Project": "beta"}).status_code == 409
        assert client.post("/tasks/impl-identity/start", json={"nonce": "x"}).status_code == 428
        assert client.post("/tasks/impl-identity/start", json={"nonce": "x"}, headers={
            "X-Agent-Crew-Project": "beta"}).status_code == 409
        assert client.get("/tasks/impl-identity").json()["status"] == "pending"
        claimed = client.get("/tasks/next?role=implementer", headers={
            "X-Agent-Crew-Project": "alpha"})
        assert claimed.status_code == 200
        assert claimed.json()["task_id"] == "impl-identity"


def test_http_enqueue_requires_matching_project_before_insert(tmp_path):
    with TestClient(create_app(str(tmp_path / "tasks.db"), project="alpha",
                               identity_required=True, watchdog_disabled=True,
                               anomaly_disabled=True)) as client:
        task = {"task_id": "impl-http", "task_type": "implement",
                "description": "work", "project": "alpha"}
        assert client.post("/tasks", json=task).status_code == 428
        assert client.post("/tasks", json=task, headers={
            "X-Agent-Crew-Project": "beta"}).status_code == 409
        assert client.get("/tasks/impl-http").status_code == 404
        assert client.post("/tasks", json=task, headers={
            "X-Agent-Crew-Project": "alpha"}).status_code == 201


def test_loop_http_enqueue_asserts_project():
    class Response(BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            self.close()
    with patch("agent_crew.loop.urllib.request.urlopen", return_value=Response(
            b'{"task_id":"impl-http"}')) as urlopen:
        assert _post_task_http(8102, TaskRequest(
            task_id="impl-http", task_type="implement", description="work",
            project="alpha")) == "impl-http"
    assert urlopen.call_args.args[0].get_header("X-agent-crew-project") == "alpha"


def test_discuss_verifies_project_before_opening_queue(tmp_path):
    state_dir = tmp_path / "alpha"
    state_dir.mkdir()
    db = state_dir / "tasks.db"
    (state_dir / "state.json").write_text(json.dumps({
        "project": "alpha", "port": 8102, "db": str(db),
        "agents": ["claude"], "pane_map": {"claude": "%1"},
    }))
    with patch("agent_crew.cli._port_listening", return_value=True), \
         patch("agent_crew.project_identity.verify_server_identity",
               side_effect=ProjectIdentityError("foreign server")) as verify, \
         patch("agent_crew.cli._writable_queue") as queue:
        result = CliRunner().invoke(crew, ["discuss", "topic", "--project", "alpha",
                                           "--base", str(tmp_path), "--nowait"])
    assert result.exit_code != 0
    assert "foreign server" in result.output
    verify.assert_called_once()
    queue.assert_not_called()


def test_cli_enqueue_verifies_project_and_sends_header(tmp_path):
    state_dir = tmp_path / "alpha"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(json.dumps({
        "project": "alpha", "port": 8102, "db": str(state_dir / "tasks.db"),
    }))
    response = MagicMock(status=201)
    response.__enter__.return_value = response
    with patch("agent_crew.cli._verify_project_server") as verify, \
         patch("urllib.request.urlopen", return_value=response) as urlopen:
        result = CliRunner().invoke(crew, ["enqueue", "implement", "work",
                                           "--project", "alpha", "--base", str(tmp_path)])
    assert result.exit_code == 0, result.output
    verify.assert_called_once_with(8102, "alpha")
    assert urlopen.call_args.args[0].get_header("X-agent-crew-project") == "alpha"


def test_generated_protocol_and_pushed_result_assert_project():
    text = generate("implementer", "alpha", 8102, delivery="push")
    assert "/health" in text
    assert "Do not receive a task or submit a result" in text
    assert "X-Agent-Crew-Project: alpha" in text
    start = text.index("curl -sS -X POST http://127.0.0.1:8102/tasks/<task_id>/result")
    command = text[start:text.index("\n```", start)].replace("<task_id>", "task-1")
    # Execute with a shell function in place of curl: this checks the real
    # argument vector without making an HTTP request.
    parsed = subprocess.run(
        ["bash", "-c", "curl() { printf '%s\\n' \"$@\"; };\n" + command],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert parsed[:9] == [
        "-sS", "-X", "POST", "http://127.0.0.1:8102/tasks/task-1/result",
        "-H", "X-Agent-Crew-Project: alpha",
        "-H", "Content-Type: application/json", "-d",
    ]
    task = TaskRequest(task_id="impl-identity", task_type="implement", description="work")
    assert "-H 'X-Agent-Crew-Project: alpha'" in _format_task_message(
        task, 8102, project="alpha")


def test_cli_identity_check_fails_closed_on_foreign_or_missing_health(monkeypatch, tmp_path):
    state_dir = tmp_path / "alpha"
    state_dir.mkdir()
    db = state_dir / "tasks.db"
    (state_dir / "state.json").write_text(json.dumps({
        "project": "alpha", "port": 8102, "db": str(db), "pane_ids": [],
    }))
    runner = CliRunner()
    for health in ("beta", None):
        def fake_verify(*_args, **_kwargs):
            raise ProjectIdentityError(f"identity verification failed: server project={health!r}")
        with patch("agent_crew.cli._port_listening", return_value=True), \
             patch("agent_crew.project_identity.verify_server_identity", side_effect=fake_verify), \
             patch("agent_crew.cli._writable_queue") as queue:
            result = runner.invoke(crew, ["run", "work", "--project", "alpha",
                                         "--base", str(tmp_path)])
        assert result.exit_code != 0
        assert "identity verification failed" in result.output
        queue.assert_not_called()


def test_health_verifier_requires_exact_project(monkeypatch):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def read(self):
            return b'{"project":"alpha"}'
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    assert verify_server_identity("http://127.0.0.1:8102", "alpha")["project"] == "alpha"
