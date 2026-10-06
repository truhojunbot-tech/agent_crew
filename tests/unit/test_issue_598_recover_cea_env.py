"""Recovery preserves the project's CEA launch environment (#598)."""

from types import SimpleNamespace

from click.testing import CliRunner

from agent_crew import cli


def _recover(tmp_path, monkeypatch, args=()):
    project_dir = tmp_path / "demo"
    project_dir.mkdir(exist_ok=True)
    state = {
        "project": "demo", "port": 19898, "session": "crew-test", "window": "0",
        "agents": [], "pane_ids": [], "worktrees": {},
        "db": str(project_dir / "tasks.db"), "server_pid": 0,
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
    return result, calls, project_dir


def test_recover_loads_cea_env_and_uses_broker_client_group(tmp_path, monkeypatch):
    project_dir = tmp_path / "demo"
    project_dir.mkdir()
    broker_dir = tmp_path / "broker"
    broker_dir.mkdir()
    secret = "secret-value-never-log"
    (project_dir / "cea.env").write_text(
        f"AGENT_CREW_CEA_MODE=enforce\nAGENT_CREW_CEA_SECRET={secret}\n"
        f"AGENT_CREW_CEA_BROKER_SOCKET={broker_dir / 'broker.sock'}\n")

    result, calls, _ = _recover(tmp_path, monkeypatch)

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    command, env = calls[0]
    assert env["AGENT_CREW_CEA_MODE"] == "enforce"
    assert env["AGENT_CREW_CEA_SECRET"] == secret
    assert command[0] == "sg"
    assert secret not in result.output
    assert secret not in (project_dir / "crew.log").read_text()


def test_recover_uses_swap_env_file_override(tmp_path, monkeypatch):
    configured = tmp_path / "configured.env"
    configured.write_text("AGENT_CREW_CEA_MODE=shadow\n")
    monkeypatch.setenv("AGENT_CREW_SWAP_CEA_ENV_FILE", str(configured))

    result, calls, _ = _recover(tmp_path, monkeypatch)

    assert result.exit_code == 0, result.output
    assert calls[0][1]["AGENT_CREW_CEA_MODE"] == "shadow"


def test_recover_without_cea_file_requires_explicit_override(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_SWAP_CEA_ENV_FILE", raising=False)
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "enforce")

    refused, calls, _ = _recover(tmp_path, monkeypatch)
    assert refused.exit_code != 0
    assert "CEA env file missing" in refused.output
    assert "--allow-no-cea" in refused.output
    assert calls == []

    allowed, calls, _ = _recover(tmp_path, monkeypatch, ("--allow-no-cea",))
    assert allowed.exit_code == 0, allowed.output
    assert len(calls) == 1
    assert "AGENT_CREW_CEA_MODE" not in calls[0][1]
