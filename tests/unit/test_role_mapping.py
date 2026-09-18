"""Tests for the provider-neutral project role mapping resolver and CLI."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from agent_crew.cli import crew
from agent_crew.server import resolve_role_to_agent


def test_explicit_role_mapping_is_returned_verbatim():
    mapping, source = resolve_role_to_agent({
        "explicit_role_to_agent": {"planner": "openai", "builder": "local"},
        "roles": [{"role": "implementer", "agent": "claude"}],
    })

    assert mapping == {"planner": "openai", "builder": "local"}
    assert source == "explicit project config"


def test_legacy_roles_overlay_the_current_defaults():
    mapping, source = resolve_role_to_agent({
        "roles": [{"role": "reviewer", "agent": "local"}],
    })

    assert mapping == {
        "implementer": "claude",
        "reviewer": "local",
        "tester": "gemini",
    }
    assert source == "legacy state.json.roles"


def test_legacy_roles_stops_at_none_and_keeps_the_valid_prefix():
    mapping, source = resolve_role_to_agent({
        "roles": [
            {"role": "reviewer", "agent": "local"},
            None,
            {"role": "reviewer", "agent": "later"},
        ],
    })

    assert mapping == {
        "implementer": "claude",
        "reviewer": "local",
        "tester": "gemini",
    }
    assert source == "legacy state.json.roles"


def test_legacy_roles_stops_at_an_unhashable_role_and_keeps_the_valid_prefix():
    mapping, source = resolve_role_to_agent({
        "roles": [
            {"role": "reviewer", "agent": "local"},
            {"role": ["reviewer"], "agent": "later"},
        ],
    })

    assert mapping == {
        "implementer": "claude",
        "reviewer": "local",
        "tester": "gemini",
    }
    assert source == "legacy state.json.roles"


def test_absent_or_empty_state_uses_hardcoded_defaults():
    for state in (None, {}):
        mapping, source = resolve_role_to_agent(state)

        assert mapping == {
            "implementer": "claude",
            "reviewer": "codex",
            "tester": "gemini",
        }
        assert source == "hardcoded default"


def test_roles_set_persists_unique_mapping_and_resolver_reloads_it(tmp_path):
    runner = CliRunner()
    result = runner.invoke(crew, [
        "roles", "set", "demo",
        "tester=gemini", "implementer=codex", "reviewer=claude",
        "--base", str(tmp_path),
    ])

    assert result.exit_code == 0, result.output
    state = json.loads((tmp_path / "demo" / "state.json").read_text())
    assert state == {
        "project": "demo",
        "explicit_role_to_agent": {
            "implementer": "codex",
            "reviewer": "claude",
            "tester": "gemini",
        },
    }
    assert resolve_role_to_agent(state) == (
        {"implementer": "codex", "reviewer": "claude", "tester": "gemini"},
        "explicit project config",
    )


def test_roles_set_retains_legacy_roles_and_show_uses_explicit_mapping(tmp_path):
    state_path = tmp_path / "legacy" / "state.json"
    state_path.parent.mkdir()
    legacy_roles = [{"role": "implementer", "agent": "legacy-agent"}]
    state_path.write_text(json.dumps({
        "project": "legacy",
        "port": 9876,
        "roles": legacy_roles,
        "other": {"keep": True},
    }))
    runner = CliRunner()

    set_result = runner.invoke(crew, [
        "roles", "set", "legacy",
        "implementer=codex", "reviewer=claude", "tester=gemini",
        "--base", str(tmp_path),
    ])
    assert set_result.exit_code == 0, set_result.output
    persisted = json.loads(state_path.read_text())
    assert persisted["roles"] == legacy_roles
    assert persisted["port"] == 9876
    assert persisted["other"] == {"keep": True}

    show_result = runner.invoke(crew, ["roles", "show", "legacy", "--base", str(tmp_path)])
    assert show_result.exit_code == 0, show_result.output
    assert show_result.output == (
        "Project: legacy\n"
        "Source: explicit project config\n"
        "implementer: codex\n"
        "reviewer: claude\n"
        "tester: gemini\n"
    )


def test_roles_show_without_state_uses_hardcoded_defaults_read_only(tmp_path):
    result = CliRunner().invoke(crew, ["roles", "show", "new-project", "--base", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert result.output == (
        "Project: new-project\n"
        "Source: hardcoded default\n"
        "implementer: claude\n"
        "reviewer: codex\n"
        "tester: gemini\n"
    )
    assert not (tmp_path / "new-project").exists()


def test_roles_set_validation_failure_does_not_mutate_existing_state(tmp_path):
    state_path = tmp_path / "demo" / "state.json"
    state_path.parent.mkdir()
    original = {"project": "demo", "roles": [{"role": "reviewer", "agent": "old"}]}
    state_path.write_text(json.dumps(original))

    result = CliRunner().invoke(crew, [
        "roles", "set", "demo",
        "implementer=codex", "reviewer=claude", "reviewer=gemini",
        "--base", str(tmp_path),
    ])

    assert result.exit_code != 0
    assert json.loads(state_path.read_text()) == original


def test_roles_show_legacy_mapping_is_read_only_and_reports_legacy_source(tmp_path):
    state_path = tmp_path / "legacy-only" / "state.json"
    state_path.parent.mkdir()
    state_path.write_text(json.dumps({
        "project": "legacy-only",
        "roles": [{"role": "reviewer", "agent": "local"}],
    }, indent=2))
    before = state_path.read_bytes()

    result = CliRunner().invoke(crew, ["roles", "show", "legacy-only", "--base", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert result.output == (
        "Project: legacy-only\n"
        "Source: legacy state.json.roles\n"
        "implementer: claude\n"
        "reviewer: local\n"
        "tester: gemini\n"
    )
    assert state_path.read_bytes() == before


def test_roles_set_invalid_assignments_leave_existing_state_unchanged(tmp_path):
    state_path = tmp_path / "demo" / "state.json"
    state_path.parent.mkdir()
    original = {"project": "demo", "other": {"keep": True}}
    state_path.write_text(json.dumps(original))
    runner = CliRunner()

    invalid_assignments = {
        "malformed": ["implementer=codex", "reviewer=claude", "tester"],
        "unknown": ["implementer=codex", "reviewer=claude", "unknown=gemini"],
        "missing": ["implementer=codex", "reviewer=claude"],
        "empty": ["implementer=codex", "reviewer=claude", "tester="],
    }
    for assignments in invalid_assignments.values():
        result = runner.invoke(crew, ["roles", "set", "demo", *assignments, "--base", str(tmp_path)])

        assert result.exit_code != 0
        assert json.loads(state_path.read_text()) == original

    result = runner.invoke(crew, [
        "roles", "set", "absent", "implementer=codex", "reviewer=claude", "tester",
        "--base", str(tmp_path),
    ])
    assert result.exit_code != 0
    assert not (tmp_path / "absent").exists()


def test_roles_set_is_idempotent_with_legacy_and_other_state_fields(tmp_path):
    state_path = tmp_path / "repeat" / "state.json"
    state_path.parent.mkdir()
    state_path.write_text(json.dumps({
        "project": "repeat",
        "roles": [{"role": "reviewer", "agent": "legacy"}],
        "other": ["preserve", 1],
    }, indent=2))
    command = [
        "roles", "set", "repeat",
        "implementer=codex", "reviewer=claude", "tester=gemini",
        "--base", str(tmp_path),
    ]
    runner = CliRunner()

    first = runner.invoke(crew, command)
    assert first.exit_code == 0, first.output
    first_state = state_path.read_bytes()

    second = runner.invoke(crew, command)
    assert second.exit_code == 0, second.output
    assert state_path.read_bytes() == first_state


def test_roles_set_mapping_survives_stale_setup_reinitialization(tmp_path):
    runner = CliRunner(env={"TMUX_PANE": "%0", "AGENT_CREW_DISPATCHER": "0"})
    mapping = {
        "implementer": "codex",
        "reviewer": "claude",
        "tester": "gemini",
    }
    set_result = runner.invoke(crew, [
        "roles", "set", "demo",
        *(f"{role}={agent}" for role, agent in mapping.items()),
        "--base", str(tmp_path),
    ])
    assert set_result.exit_code == 0, set_result.output

    def _fake_run(args, **_kwargs):
        command = args[1] if len(args) > 1 else ""
        joined = " ".join(str(arg) for arg in args)
        if command == "display-message":
            if "#S:#I" in joined:
                return MagicMock(returncode=0, stdout="crew:0\n", stderr="")
            if "window_width" in joined:
                return MagicMock(returncode=0, stdout="240\n", stderr="")
            if "pane_width" in joined:
                return MagicMock(returncode=0, stdout="80\n", stderr="")
            return MagicMock(returncode=0, stdout="crew\n", stderr="")
        if command == "split-window":
            return MagicMock(returncode=0, stdout="%99\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    server_process = MagicMock(pid=12345)
    with patch("agent_crew.cli.setup_module.validate_git_repo", return_value=True), \
         patch("agent_crew.cli._port_listening", side_effect=[False, True]), \
         patch("agent_crew.cli.setup_module.find_free_port", return_value=19999), \
         patch("agent_crew.cli.setup_module.write_port_file"), \
         patch("agent_crew.cli.setup_module.create_worktrees", return_value={
             "claude": str(tmp_path / "wt-claude"),
         }), \
         patch("agent_crew.cli.setup_module.write_instruction_files"), \
         patch("agent_crew.cli.setup_module.write_sessions_json"), \
         patch("agent_crew.cli.setup_module.write_mcp_configs"), \
         patch("agent_crew.cli.subprocess.run", side_effect=_fake_run), \
         patch("agent_crew.cli.subprocess.Popen", return_value=server_process), \
         patch("agent_crew.cli.setup_module.pretrust_claude_worktree"), \
         patch("agent_crew.cli.setup_module.start_agents_in_panes"), \
         patch("os.getcwd", return_value=str(tmp_path)):
        setup_result = runner.invoke(crew, [
            "setup", "demo", "--agents", "claude", "--base", str(tmp_path),
        ])

    assert setup_result.exit_code == 0, setup_result.output
    state = json.loads((tmp_path / "demo" / "state.json").read_text())
    assert state["explicit_role_to_agent"] == mapping


def test_cli_help_import_is_side_effect_free_with_invalid_server_port(tmp_path):
    src_dir = Path(__file__).resolve().parents[2] / "src"
    env = {
        **os.environ,
        "PYTHONPATH": str(src_dir),
        "AGENT_CREW_PORT": "definitely-not-an-integer",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from click.testing import CliRunner; "
            "from agent_crew.cli import crew; "
            "import sys; "
            "result = CliRunner().invoke(crew, ['--help']); "
            "assert result.exit_code == 0, result.output; "
            "assert 'agent_crew.server' not in sys.modules",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
