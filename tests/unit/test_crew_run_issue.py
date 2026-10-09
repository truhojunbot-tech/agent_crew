"""crew run passes an explicitly supplied issue number to implement tasks."""

from click.testing import CliRunner

from agent_crew.cli import crew
from agent_crew.protocol import TaskResult
from agent_crew.queue import TaskQueue


def _run(monkeypatch, tmp_path, *extra):
    db = tmp_path / "tasks.db"
    monkeypatch.setattr(
        TaskQueue, "get_result",
        lambda _queue, task_id: TaskResult(
            task_id=task_id, status="failed", summary="fixture stops after enqueue"
        ),
    )
    result = CliRunner().invoke(
        crew, ["run", "implement issue work", "--db", str(db),
               "--branch", "fix/issue", "--no-tester", *extra],
    )
    tasks = TaskQueue(str(db)).list_tasks() if db.exists() else []
    return result, tasks


def test_run_issue_flag_sets_implement_context(monkeypatch, tmp_path):
    result, tasks = _run(monkeypatch, tmp_path, "--issue", "560")

    assert result.exit_code == 0, result.output
    assert len(tasks) == 1
    assert tasks[0].context["issue"] == 560
    assert tasks[0].context["issue_number"] == 560
    assert isinstance(tasks[0].context["issue"], int)
    assert "without --issue" not in result.output


def test_run_without_issue_keeps_context_absent(monkeypatch, tmp_path):
    result, tasks = _run(monkeypatch, tmp_path)

    assert result.exit_code == 0, result.output
    assert len(tasks) == 1
    assert "issue" not in tasks[0].context
    assert "issue_number" not in tasks[0].context
    assert "without --issue" in result.output


def test_run_issue_rejects_zero(monkeypatch, tmp_path):
    result, tasks = _run(monkeypatch, tmp_path, "--issue", "0")

    assert result.exit_code != 0
    assert tasks == []


def test_created_issue_is_recorded_without_missing_issue_warning(monkeypatch, tmp_path):
    from agent_crew import github

    monkeypatch.setattr(github, "check_gh_installed", lambda: True)
    monkeypatch.setattr(github, "create_issue", lambda **kwargs: 654)
    result, tasks = _run(monkeypatch, tmp_path, "--create-issue", "--repo", "o/r")

    assert result.exit_code == 0, result.output
    assert tasks[0].context["issue_number"] == 654
    assert "without --issue" not in result.output
