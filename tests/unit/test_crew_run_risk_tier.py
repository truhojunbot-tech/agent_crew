"""The crew run risk tier is durable task context, with an optional env default."""

import pytest
from click.testing import CliRunner

from agent_crew.cli import crew
from agent_crew.protocol import TaskResult
from agent_crew.queue import TaskQueue


def _run(monkeypatch, tmp_path, *extra):
    db = tmp_path / "tasks.db"
    monkeypatch.setattr(
        TaskQueue, "get_result",
        lambda _queue, task_id: TaskResult(task_id=task_id, status="failed",
                                          summary="fixture stops after enqueue"),
    )
    result = CliRunner().invoke(
        crew, ["run", "implement a routine change", "--db", str(db),
               "--branch", "fix/risk-tier", "--no-tester", *extra],
    )
    tasks = TaskQueue(str(db)).list_tasks() if db.exists() else []
    return result, tasks, TaskQueue(str(db)) if db.exists() else None


def test_option_sets_implement_context(monkeypatch, tmp_path):
    result, tasks, _ = _run(monkeypatch, tmp_path, "--risk-tier", "2")
    assert result.exit_code == 0, result.output
    assert len(tasks) == 1
    assert tasks[0].context["risk_tier"] == 2


def test_env_default_sets_implement_context(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CREW_DEFAULT_RISK_TIER", "1")
    result, tasks, _ = _run(monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    assert tasks[0].context["risk_tier"] == 1


def test_option_beats_env_default(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CREW_DEFAULT_RISK_TIER", "invalid")
    result, tasks, _ = _run(monkeypatch, tmp_path, "--risk-tier", "3")
    assert result.exit_code == 0, result.output
    assert tasks[0].context["risk_tier"] == 3


@pytest.mark.parametrize("raw", ["", "4", "-1", "1.5", "routine"])
def test_invalid_env_is_a_usage_error(monkeypatch, tmp_path, raw):
    monkeypatch.setenv("AGENT_CREW_DEFAULT_RISK_TIER", raw)
    result, tasks, _ = _run(monkeypatch, tmp_path)
    assert result.exit_code != 0
    assert "AGENT_CREW_DEFAULT_RISK_TIER" in result.output
    assert tasks == []


def test_absent_tier_preserves_context(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_CREW_DEFAULT_RISK_TIER", raising=False)
    result, tasks, _ = _run(monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    assert "risk_tier" not in tasks[0].context


def test_tier_one_records_explicit_high_declaration(monkeypatch, tmp_path):
    result, tasks, queue = _run(monkeypatch, tmp_path, "--risk-tier", "1")
    assert result.exit_code == 0, result.output
    # Attribution rows are written at dispatch; enqueue persists the declaration
    # in task context, which dispatch later copies into attribution unchanged.
    declaration = queue.get_task_context(tasks[0].task_id)["risk_declaration"]
    assert declaration["declaration_source"] == "explicit"
    assert declaration["confidence"] == "high"


def test_cli_review_and_test_inherit_declared_root_tier(monkeypatch, tmp_path):
    db = tmp_path / "tasks.db"

    def result_for(_queue, task_id):
        if task_id.startswith("impl-"):
            return TaskResult(task_id, "completed", "implemented",
                              branch="fix/risk-tier", commit="a" * 40)
        if task_id.startswith("review-"):
            return TaskResult(task_id, "completed", "approved", verdict="approve")
        return TaskResult(task_id, "completed", "tests passed")

    monkeypatch.setattr(TaskQueue, "get_result", result_for)
    result = CliRunner().invoke(crew, [
        "run", "implement a routine change", "--db", str(db),
        "--branch", "fix/risk-tier", "--risk-tier", "2",
    ])
    assert result.exit_code == 0, result.output
    tasks = TaskQueue(str(db)).list_tasks()
    assert {task.task_type for task in tasks} == {"implement", "review", "test"}
    root = next(task for task in tasks if task.task_type == "implement")
    for successor in (task for task in tasks if task.task_type in {"review", "test"}):
        assert successor.context["risk_tier"] == 2
        declaration = successor.context["risk_declaration"]
        assert declaration["declaration_source"] == "explicit"
        assert declaration["broad_architecture_change"] is True
        assert declaration["broad_architecture_change"] is root.context[
            "risk_declaration"]["broad_architecture_change"]


def test_help_documents_risk_tier():
    result = CliRunner().invoke(crew, ["run", "--help"])
    assert result.exit_code == 0
    assert "--risk-tier" in result.output
    assert "AGENT_CREW_DEFAULT_RISK_TIER" in result.output
