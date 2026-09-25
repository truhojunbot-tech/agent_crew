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


def test_codex_cap_project_state_survives_setup_and_recover(monkeypatch):
    from agent_crew.cli import _codex_context_cap_state, _codex_context_cap_launch_value

    monkeypatch.delenv("AGENT_CREW_CODEX_CONTEXT_MAX_MB", raising=False)
    state = _codex_context_cap_state("agent_crew", {})
    assert state["codex_context_max_mb"] == 8
    assert _codex_context_cap_launch_value(state) == ("8", "project_state")
    assert _codex_context_cap_state("agent_crew", state) == state
    assert _codex_context_cap_launch_value(_codex_context_cap_state("other", {})) == (
        "64", "built_in_default")

    monkeypatch.setenv("AGENT_CREW_CODEX_CONTEXT_MAX_MB", "12")
    assert _codex_context_cap_launch_value(state) == ("12", "environment")


def test_server_codex_cap_precedence(tmp_path, monkeypatch):
    import json
    from agent_crew.server import _codex_context_cap_mb

    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"codex_context_max_mb": 8}))
    monkeypatch.delenv("AGENT_CREW_CODEX_CONTEXT_MAX_MB", raising=False)
    assert _codex_context_cap_mb(state_file) == 8
    state_file.write_text("{}")
    assert _codex_context_cap_mb(state_file) == 64
    monkeypatch.setenv("AGENT_CREW_CODEX_CONTEXT_MAX_MB", "12")
    assert _codex_context_cap_mb(state_file) == 12
