"""Recovery preserves the project's CEA launch environment (#598)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from agent_crew import cli


def _recover(tmp_path, monkeypatch, args=(), *, cea_expected=True):
    project_dir = tmp_path / "demo"
    project_dir.mkdir(exist_ok=True)
    state = {
        "project": "demo", "port": 19898, "session": "crew-test", "window": "0",
        "agents": [], "pane_ids": [], "worktrees": {},
        "db": str(project_dir / "tasks.db"), "server_pid": 0,
        "cea_env_expected": cea_expected,
    }
    calls = []
    monkeypatch.setattr(cli, "_read_state", lambda *_: state)
    monkeypatch.setattr(cli, "_write_state", lambda *_: None)
    monkeypatch.setattr(cli, "_port_listening", lambda _port, timeout: timeout > 1)
    monkeypatch.setattr(cli, "_tmux_target_valid", lambda *_: True)
    monkeypatch.setattr(cli, "_validate_pane_map", lambda *_: {"valid": True})
    monkeypatch.setattr(cli.setup_module, "write_instruction_files", lambda *_: None)
    monkeypatch.setattr(cli.setup_module, "write_mcp_configs", lambda *_: None)
    monkeypatch.setattr(cli.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=0, stdout="", stderr=""))

    def popen(command, **kwargs):
        calls.append((command, kwargs["env"]))
        return SimpleNamespace(pid=12345)

    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    result = CliRunner().invoke(cli.crew, ["recover", "demo", "--base", str(tmp_path), *args])
    return result, calls, project_dir, state


def test_recover_loads_cea_env_and_uses_broker_client_group(tmp_path, monkeypatch):
    project_dir = tmp_path / "demo"
    project_dir.mkdir()
    broker_dir = tmp_path / "broker"
    broker_dir.mkdir()
    secret = "secret-value-never-log"
    (project_dir / "cea.env").write_text(
        f"AGENT_CREW_CEA_MODE=enforce\nAGENT_CREW_CEA_SECRET={secret}\n"
        f"AGENT_CREW_CEA_BROKER_SOCKET={broker_dir / 'broker.sock'}\n")

    monkeypatch.setattr(cli, "_server_listener_pid", lambda _port: 54321, raising=False)
    result, calls, _, state = _recover(tmp_path, monkeypatch)

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    command, env = calls[0]
    assert env["AGENT_CREW_CEA_MODE"] == "enforce"
    assert env["AGENT_CREW_CEA_SECRET"] == secret
    assert command[0] == "sg"
    assert state["server_pid"] == 54321
    assert secret not in result.output
    assert secret not in (project_dir / "crew.log").read_text()


def test_recover_uses_swap_env_file_override(tmp_path, monkeypatch):
    configured = tmp_path / "configured.env"
    configured.write_text("AGENT_CREW_CEA_MODE=shadow\n")
    monkeypatch.setenv("AGENT_CREW_SWAP_CEA_ENV_FILE", str(configured))

    result, calls, _, _ = _recover(tmp_path, monkeypatch)

    assert result.exit_code == 0, result.output
    assert calls[0][1]["AGENT_CREW_CEA_MODE"] == "shadow"


def test_recover_without_cea_file_requires_explicit_override(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_SWAP_CEA_ENV_FILE", raising=False)
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "enforce")

    refused, calls, _, _ = _recover(tmp_path, monkeypatch)
    assert refused.exit_code != 0
    assert "CEA env file missing" in refused.output
    assert "--allow-no-cea" in refused.output
    assert calls == []

    allowed, calls, _, _ = _recover(tmp_path, monkeypatch, ("--allow-no-cea",))
    assert allowed.exit_code == 0, allowed.output
    assert len(calls) == 1
    assert "AGENT_CREW_CEA_MODE" not in calls[0][1]


def test_recover_without_prior_cea_allows_missing_file(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_SWAP_CEA_ENV_FILE", raising=False)
    result, calls, _, _ = _recover(tmp_path, monkeypatch, cea_expected=False)
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert not any(key.startswith("AGENT_CREW_CEA_") for key in calls[0][1])


def test_listener_pid_identifies_uvicorn_behind_sg(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=0, stdout=(
                            'LISTEN 0 128 127.0.0.1:19898 0.0.0.0:* '
                            'users:(("python3",pid=54321,fd=7))\n')))
    assert cli._server_listener_pid(19898) == 54321


def test_setup_refuses_missing_prior_cea_before_creating_state_or_panes(tmp_path):
    state = {"port": 19898, "pane_ids": [], "cea_env_expected": True}
    project_dir = tmp_path / "demo"
    with patch.object(cli.setup_module, "validate_git_repo", return_value=True), \
         patch.object(cli.setup_module, "require_exclusive_port"), \
         patch.object(cli, "_read_state", return_value=state), \
         patch.object(cli, "_port_listening", return_value=False), \
         patch.object(cli.setup_module, "create_worktrees") as worktrees, \
         patch.object(cli, "_write_state") as write_state, \
         patch.object(cli.subprocess, "run", return_value=MagicMock(
             returncode=0, stdout="crew:0\n", stderr="")) as run:
        result = CliRunner(env={"TMUX_PANE": "%90"}).invoke(
            cli.crew, ["setup", "demo", "--base", str(tmp_path)])

    assert result.exit_code != 0
    assert "CEA env file missing" in result.output
    worktrees.assert_not_called()
    write_state.assert_not_called()
    assert not any(call.args[0][:2] == ["tmux", "split-window"] for call in run.call_args_list)
    assert not project_dir.exists()


def test_setup_records_listener_pid_behind_sg(tmp_path, monkeypatch):
    project_dir = tmp_path / "demo"
    project_dir.mkdir()
    broker_dir = tmp_path / "broker"
    broker_dir.mkdir()
    (project_dir / "cea.env").write_text(
        f"AGENT_CREW_CEA_BROKER_SOCKET={broker_dir / 'broker.sock'}\n")
    written = []
    monkeypatch.setattr(cli, "_write_state", lambda _base, _project, state:
                        written.append(dict(state)))
    monkeypatch.setattr(cli, "_resolve_tmux_window", lambda *_: ("crew", "0"))
    monkeypatch.setattr(cli, "_server_listener_pid", lambda _port: 54321)
    monkeypatch.setattr(cli, "_port_listening", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(cli.setup_module, "validate_git_repo", lambda *_: True)
    monkeypatch.setattr(cli.setup_module, "find_free_port", lambda *_args, **_kwargs: 19898)
    monkeypatch.setattr(cli.setup_module, "create_worktrees", lambda *_args, **_kwargs:
                        {"codex": str(tmp_path / "wt")})
    for name in ("write_port_file", "write_instruction_files", "write_sessions_json",
                 "write_mcp_configs", "pretrust_claude_worktree", "start_agents_in_panes"):
        monkeypatch.setattr(cli.setup_module, name, lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.subprocess, "run", lambda args, **_kwargs:
                        SimpleNamespace(returncode=0, stdout=(
                            "%999\n" if "split-window" in args else "200\n"), stderr=""))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *_args, **_kwargs:
                        SimpleNamespace(pid=12345))
    monkeypatch.setattr(cli, "_read_state", lambda *_args: written[-1] if written else None)

    result = CliRunner(env={"TMUX_PANE": "%90"}).invoke(
        cli.crew, ["setup", "demo", "--agents", "codex", "--base", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert written[-1]["server_pid"] == 54321
    assert not any(state.get("server_pid") == 12345 for state in written)
