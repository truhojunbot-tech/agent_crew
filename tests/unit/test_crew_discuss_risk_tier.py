"""The discussion loop validates its future implement root before panel work."""

from click.testing import CliRunner

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
