"""#359: Context Pack launch setting is durable and observable."""


def test_durable_state_controls_clean_environment(monkeypatch):
    from agent_crew.cli import _context_pack_launch_value

    monkeypatch.delenv("AGENT_CREW_CONTEXT_PACK", raising=False)
    assert _context_pack_launch_value({"context_pack_enabled": True}) == ("1", "project_state")
    assert _context_pack_launch_value({"context_pack_enabled": False}) == ("0", "project_state")


def test_explicit_environment_override_wins_in_both_directions(monkeypatch):
    from agent_crew.cli import _context_pack_launch_value

    monkeypatch.setenv("AGENT_CREW_CONTEXT_PACK", "0")
    assert _context_pack_launch_value({"context_pack_enabled": True}) == ("0", "environment")
    monkeypatch.setenv("AGENT_CREW_CONTEXT_PACK", "1")
    assert _context_pack_launch_value({"context_pack_enabled": False}) == ("1", "environment")


def test_absent_state_defaults_off_without_crashing(monkeypatch):
    from agent_crew.cli import _context_pack_launch_value

    monkeypatch.delenv("AGENT_CREW_CONTEXT_PACK", raising=False)
    assert _context_pack_launch_value(None) == ("0", "project_state")


def test_server_logs_effective_context_pack_setting(monkeypatch, tmp_db, caplog):
    from agent_crew.server import create_app

    monkeypatch.setenv("AGENT_CREW_CONTEXT_PACK", "1")
    create_app(tmp_db, watchdog_disabled=True)
    assert "Context Pack effective enabled=True" in caplog.text
