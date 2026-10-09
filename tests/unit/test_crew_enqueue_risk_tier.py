"""The standalone enqueue command declares root risk before writing a task."""

from click.testing import CliRunner

from agent_crew.cli import crew
from agent_crew.queue import TaskQueue


def invoke(tmp_path, *args):
    db = str(tmp_path / "tasks.db")
    result = CliRunner().invoke(
        crew, ["enqueue", "review", "review work", "--db", db, *args])
    return result, TaskQueue(db)


def test_root_without_tier_fails_before_enqueue(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_DEFAULT_RISK_TIER", raising=False)
    result, queue = invoke(tmp_path)
    assert result.exit_code != 0
    assert "--risk-tier 0-3" in result.output
    assert queue.list_tasks() == []


def test_prev_task_id_still_requires_tier(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_DEFAULT_RISK_TIER", raising=False)
    result, queue = invoke(tmp_path, "--prev-task-id", "impl-1")
    assert result.exit_code != 0
    assert "--risk-tier 0-3" in result.output
    assert queue.list_tasks() == []


def test_root_explicit_tier(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_DEFAULT_RISK_TIER", raising=False)
    result, queue = invoke(tmp_path, "--risk-tier", "0")
    assert result.exit_code == 0, result.output
    assert queue.list_tasks()[0].context["risk_tier"] == 0


def test_root_env_default(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DEFAULT_RISK_TIER", "2")
    result, queue = invoke(tmp_path)
    assert result.exit_code == 0, result.output
    assert queue.list_tasks()[0].context["risk_tier"] == 2


def test_invalid_default_fails_before_enqueue(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DEFAULT_RISK_TIER", "4")
    result, queue = invoke(tmp_path)
    assert result.exit_code != 0
    assert "--risk-tier 0-3" in result.output
    assert queue.list_tasks() == []
