"""Recovery must bind tmux pane IDs to the saved project session and window."""

import json
from types import SimpleNamespace

from click.testing import CliRunner

from agent_crew import cli


def _state(base, project, agents=("claude", "codex")):
    project_dir = base / project
    project_dir.mkdir()
    worktrees = {}
    for agent in agents:
        wt = project_dir / agent
        wt.mkdir()
        worktrees[agent] = str(wt)
    state = {
        "project": project, "port": 19000, "session": f"crew_{project}",
        "window": "0", "agents": list(agents), "pane_ids": ["%0", "%1"],
        "pane_map": {"implementer": "%0", "reviewer": "%1"},
        "worktrees": worktrees, "db": str(project_dir / "tasks.db"),
        "server_pid": 1, "dispatcher_mode": True,
    }
    (project_dir / "state.json").write_text(json.dumps(state))
    return state


def _result(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _prepare(monkeypatch):
    monkeypatch.setattr(cli, "_port_listening", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(cli.setup_module, "write_instruction_files", lambda *_: None)
    monkeypatch.setattr(cli.setup_module, "write_mcp_configs", lambda *_: None)
    monkeypatch.setattr(cli.setup_module, "start_log_viewers_in_panes", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_validate_pane_map", lambda *_: {"valid": True})
    monkeypatch.setattr(cli, "_detect_dead_agent_panes", lambda *_args, **_kwargs: [])


def test_recover_recreates_two_crews_in_their_own_sessions_after_tmux_restart(tmp_path, monkeypatch):
    _prepare(monkeypatch)
    for project in ("alpha", "beta"):
        _state(tmp_path, project)
    sessions = {"caller": ["%0", "%1"]}
    owner = {"%0": "caller:0", "%1": "caller:0"}
    next_id = 2
    commands = []

    def tmux(args, **_kwargs):
        nonlocal next_id
        commands.append(args)
        command = args[1]
        if command == "has-session":
            return _result(0 if args[-1] in sessions else 1)
        if command == "new-session":
            session = args[args.index("-s") + 1]
            pane = f"%{next_id}"
            next_id += 1
            sessions[session] = [pane]
            owner[pane] = f"{session}:0"
            return _result(stdout=pane + "\n")
        if command == "split-window":
            session = args[args.index("-t") + 1].split(":")[0]
            pane = f"%{next_id}"
            next_id += 1
            sessions[session].append(pane)
            owner[pane] = f"{session}:0"
            return _result(stdout=pane + "\n")
        if command == "select-layout":
            return _result()
        if command == "display-message":
            target = args[args.index("-t") + 1]
            return _result(stdout=owner.get(target, "") + "\n")
        raise AssertionError(args)

    monkeypatch.setattr(cli.subprocess, "run", tmux)
    monkeypatch.setattr(cli, "_resolve_tmux_window", lambda *_: (_ for _ in ()).throw(
        AssertionError("recover must not use caller session")))
    for project in ("alpha", "beta"):
        result = CliRunner().invoke(cli.crew, ["recover", project, "--base", str(tmp_path)])
        assert result.exit_code == 0, result.output
        state = json.loads((tmp_path / project / "state.json").read_text())
        assert state["session"] == f"crew_{project}"
        assert state["pane_ids"] == sessions[f"crew_{project}"]
        assert len(state["pane_ids"]) == 2
        assert state["pane_map"] == {}
    assert sessions["caller"] == ["%0", "%1"]
    assert [cmd[cmd.index("-t") + 1] for cmd in commands if cmd[1] == "split-window"] == [
        "crew_alpha:0", "crew_beta:0",
    ]


def test_recover_replaces_foreign_pane_even_when_id_is_alive(tmp_path, monkeypatch):
    _prepare(monkeypatch)
    _state(tmp_path, "alpha", agents=("claude",))
    calls = []

    def tmux(args, **_kwargs):
        calls.append(args)
        command = args[1]
        if command == "has-session":
            return _result()
        if command == "display-message":
            target = args[args.index("-t") + 1]
            fmt = args[-1]
            if target == "%0":
                return _result(stdout=("%0" if fmt == "#{pane_id}" else "other:0") + "\n")
            return _result(stdout="crew_alpha\n")
        if command == "split-window":
            return _result(stdout="%8\n")
        return _result()

    monkeypatch.setattr(cli.subprocess, "run", tmux)
    result = CliRunner().invoke(cli.crew, ["recover", "alpha", "--base", str(tmp_path)])
    assert result.exit_code == 0, result.output
    state = json.loads((tmp_path / "alpha" / "state.json").read_text())
    assert state["pane_ids"] == ["%8"]
    assert set(state["pane_map"].values()) == {"%8"}
    assert any(cmd[1] == "split-window" for cmd in calls)


def test_failed_split_exits_without_success_and_clears_stale_id(tmp_path, monkeypatch):
    _prepare(monkeypatch)
    _state(tmp_path, "alpha", agents=("claude",))

    def tmux(args, **_kwargs):
        command = args[1]
        if command == "has-session":
            return _result()
        if command == "display-message":
            target = args[args.index("-t") + 1]
            fmt = args[-1]
            if target == "%0":
                return _result(stdout=("%0" if fmt == "#{pane_id}" else "other:0") + "\n")
            return _result(stdout="crew_alpha\n")
        if command == "split-window":
            return _result(1, stderr="no space for new pane")
        return _result()

    monkeypatch.setattr(cli.subprocess, "run", tmux)
    result = CliRunner().invoke(cli.crew, ["recover", "alpha", "--base", str(tmp_path)])
    assert result.exit_code != 0
    assert "no space for new pane" in result.output
    assert "Recovered:" not in result.output
    assert "Nothing to recover" not in result.output
    state = json.loads((tmp_path / "alpha" / "state.json").read_text())
    assert state["pane_ids"] == [""]
    assert state["pane_map"] == {}


def test_failed_split_in_new_session_does_not_restore_old_pane_ids(tmp_path, monkeypatch):
    _prepare(monkeypatch)
    _state(tmp_path, "alpha")

    def tmux(args, **_kwargs):
        command = args[1]
        if command == "has-session":
            return _result(1)
        if command == "new-session":
            return _result(stdout="%8\n")
        if command == "split-window":
            return _result(1, stderr="no space for new pane")
        if command == "kill-session":
            return _result()
        raise AssertionError(args)

    monkeypatch.setattr(cli.subprocess, "run", tmux)
    result = CliRunner().invoke(cli.crew, ["recover", "alpha", "--base", str(tmp_path)])
    assert result.exit_code != 0
    assert "no space for new pane" in result.output
    assert "Recovered:" not in result.output
    assert "Nothing to recover" not in result.output
    state = json.loads((tmp_path / "alpha" / "state.json").read_text())
    assert state["pane_ids"] == []
    assert state["pane_map"] == {}
