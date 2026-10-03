"""CLI declarations reach the existing artifact contract without changing defaults."""

import hashlib
import json
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from agent_crew.cli import crew
from agent_crew.pipeline import ARTIFACT_KINDS
from agent_crew.protocol import TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _run(monkeypatch, tmp_path, *extra):
    db = tmp_path / "run.db"
    monkeypatch.setattr(
        TaskQueue, "get_result",
        lambda _queue, task_id: TaskResult(task_id=task_id, status="failed",
                                          summary="fixture stops after enqueue"),
    )
    result = CliRunner().invoke(crew, [
        "run", "measure read-only inventory", "--db", str(db),
        "--branch", "fix/artifact-contract", "--no-tester", *extra,
    ])
    tasks = TaskQueue(str(db)).list_tasks() if db.exists() else []
    return result, tasks


def _enqueue(tmp_path, *extra, task_id="impl-report"):
    db = tmp_path / "enqueue.db"
    result = CliRunner().invoke(crew, [
        "enqueue", "implement", "measure read-only inventory", "--db", str(db),
        "--task-id", task_id, *extra,
    ])
    tasks = TaskQueue(str(db)).list_tasks() if db.exists() else []
    return result, tasks, db


@pytest.mark.parametrize("kind", ARTIFACT_KINDS)
def test_run_sets_artifact_kind_on_implement_root(monkeypatch, tmp_path, kind):
    result, tasks = _run(monkeypatch, tmp_path, "--artifact-kind", kind)
    assert result.exit_code == 0, result.output
    assert len(tasks) == 1
    assert tasks[0].task_type == "implement"
    assert tasks[0].context["artifact_kind"] == kind


@pytest.mark.parametrize("kind", ARTIFACT_KINDS)
def test_enqueue_sets_artifact_kind(tmp_path, kind):
    result, tasks, _ = _enqueue(tmp_path, "--artifact-kind", kind)
    assert result.exit_code == 0, result.output
    assert len(tasks) == 1
    assert tasks[0].context["artifact_kind"] == kind


def test_enqueue_http_carries_artifact_kind(tmp_path, monkeypatch):
    state_dir = tmp_path / "project"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(json.dumps({
        "db": str(state_dir / "tasks.db"), "port": 9999,
    }))
    sent = {}

    def urlopen(request, timeout=10):
        sent.update(json.loads(request.data.decode()))
        response = MagicMock()
        response.status = 201
        response.__enter__.return_value = response
        return response

    monkeypatch.setattr("agent_crew.cli._verify_project_server", lambda *_a: None)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    result = CliRunner().invoke(crew, [
        "enqueue", "implement", "inventory", "--project", "project",
        "--base", str(tmp_path), "--artifact-kind", "report",
    ])
    assert result.exit_code == 0, result.output
    assert sent["context"]["artifact_kind"] == "report"


def test_absent_option_preserves_context_without_artifact_kind(monkeypatch, tmp_path):
    run_result, run_tasks = _run(monkeypatch, tmp_path)
    enqueue_result, enqueue_tasks, _ = _enqueue(tmp_path)
    assert run_result.exit_code == enqueue_result.exit_code == 0
    assert "artifact_kind" not in run_tasks[0].context
    assert "artifact_kind" not in enqueue_tasks[0].context


@pytest.mark.parametrize("command", ["run", "enqueue"])
def test_invalid_artifact_kind_is_rejected_by_click(tmp_path, command):
    db = tmp_path / "invalid.db"
    args = (["run", "work", "--db", str(db)] if command == "run" else
            ["enqueue", "implement", "work", "--db", str(db)])
    result = CliRunner().invoke(crew, [*args, "--artifact-kind", "none"])
    assert result.exit_code != 0
    assert "Invalid value for '--artifact-kind'" in result.output
    assert not db.exists()


def test_enqueue_report_contract_is_verified_by_server(tmp_path):
    report = "# Read-only measurement\n\nThree records matched.\n"
    sha = hashlib.sha256(report.encode()).hexdigest()
    first, _, db = _enqueue(tmp_path, "--artifact-kind", "report", task_id="impl-valid")
    second, _, _ = _enqueue(tmp_path, "--artifact-kind", "report", task_id="impl-missing")
    assert first.exit_code == second.exit_code == 0

    app = create_app(str(db), pane_map={}, worktree_map={}, watchdog_disabled=True,
                     anomaly_disabled=True)
    with TestClient(app) as client:
        valid = client.post("/tasks/impl-valid/result", json={
            "task_id": "impl-valid", "status": "completed", "summary": "measurement done",
            "artifact": {"body": report, "sha256": sha},
        })
        missing = client.post("/tasks/impl-missing/result", json={
            "task_id": "impl-missing", "status": "completed", "summary": "measurement done",
        })

    queue = TaskQueue(str(db))
    assert valid.status_code == 200, valid.text
    assert valid.json().get("held") is None
    assert queue.get_task("impl-valid").status == "completed"
    assert queue.get_task_context("impl-valid")["result_artifact"]["kind"] == "report"
    assert missing.status_code == 200, missing.text
    assert missing.json()["held"] == "no_artifact"
    assert queue.get_task("impl-missing").status == "failed"


def test_help_explains_report_contract_and_cascade():
    run_help = CliRunner().invoke(crew, ["run", "--help"])
    enqueue_help = CliRunner().invoke(crew, ["enqueue", "--help"])
    assert run_help.exit_code == enqueue_help.exit_code == 0
    assert "--artifact-kind" in run_help.output
    assert "post-verification cascade" in run_help.output
    assert "--artifact-kind" in enqueue_help.output
