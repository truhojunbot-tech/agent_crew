"""The discussion loop validates its future implement root before panel work."""

from click.testing import CliRunner
from types import SimpleNamespace

from agent_crew.cli import crew


def test_then_run_without_tier_refuses_before_panel(tmp_path, monkeypatch):
    import agent_crew.discussion as discussion

    monkeypatch.delenv("AGENT_CREW_DEFAULT_RISK_TIER", raising=False)
    calls = []
    monkeypatch.setattr(discussion, "enqueue_panel_tasks",
                        lambda *a, **k: calls.append("panel"))
    result = CliRunner().invoke(crew, ["discuss", "topic", "--then-run",
                                       "--db", str(tmp_path / "tasks.db"),
                                       "--agents", "claude"])
    assert result.exit_code != 0
    assert "--risk-tier 0-3" in result.output
    assert calls == []


def test_then_run_passes_env_tier_to_implement(tmp_path, monkeypatch):
    import agent_crew.discussion as discussion
    import agent_crew.loop as loop

    monkeypatch.setenv("AGENT_CREW_DEFAULT_RISK_TIER", "2")
    monkeypatch.setattr(discussion, "enqueue_panel_tasks", lambda *a, **k: [])
    contexts = []

    def capture(*args, **kwargs):
        contexts.append(kwargs["context"])
        raise RuntimeError("captured before dispatch")

    monkeypatch.setattr(loop, "enqueue_implement", capture)
    result = CliRunner().invoke(crew, ["discuss", "topic", "--then-run",
                                       "--db", str(tmp_path / "tasks.db"),
                                       "--agents", "claude"])
    assert isinstance(result.exception, RuntimeError)
    assert contexts == [{"risk_tier": 2}]


def test_then_run_passes_implement_pr_to_review(tmp_path, monkeypatch):
    import agent_crew.discussion as discussion
    import agent_crew.loop as loop
    from agent_crew.queue import TaskQueue

    monkeypatch.setenv("AGENT_CREW_DEFAULT_RISK_TIER", "2")
    monkeypatch.setattr(discussion, "enqueue_panel_tasks", lambda *a, **k: [])
    monkeypatch.setattr(loop, "enqueue_implement", lambda *a, **k: "impl-935")
    monkeypatch.setattr(TaskQueue, "get_result", lambda *a: SimpleNamespace(
        status="completed", pr_number=938, branch="fix/935", commit="a" * 40))
    contexts = []

    def capture(*args, **kwargs):
        contexts.append(kwargs["context"])
        raise RuntimeError("captured before review dispatch")

    monkeypatch.setattr(loop, "enqueue_review", capture)
    result = CliRunner().invoke(crew, ["discuss", "topic", "--then-run",
                                       "--db", str(tmp_path / "tasks.db"),
                                       "--agents", "claude"])
    assert isinstance(result.exception, RuntimeError)
    assert contexts == [{"risk_tier": 2, "pr_number": 938}]
