from unittest.mock import MagicMock, patch

from agent_crew.setup import (
    _collect_active_ports,
    _is_port_listening,
    create_worktrees,
    find_free_port,
    pretrust_claude_worktree,
    validate_git_repo,
    start_agents_in_panes,
    write_instruction_files,
    write_port_file,
    write_sessions_json,
    _get_agent_cmd,
)


# U-SE01: validate_git_repo — git repo 경로 → True (mock subprocess)
def test_u_se01_validate_git_repo_true():
    with patch("agent_crew.setup.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        assert validate_git_repo("/some/repo") is True
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "git" in args
        assert "rev-parse" in args


# U-SE02: create_worktrees — 올바른 경로 dict 반환 (explicit project_path)
def test_u_se02_create_worktrees():
    mock_result = MagicMock(returncode=0)
    def mock_isdir(path):
        # Return False to bypass backward compatibility (old paths don't exist)
        return False
    with patch("agent_crew.setup.subprocess.run", return_value=mock_result), \
         patch("agent_crew.setup.os.path.isdir", side_effect=mock_isdir):
        result = create_worktrees("myproject", "/base", ["claude", "codex"], project_path="/mock/project")
    assert set(result.keys()) == {"claude", "codex"}
    assert result["claude"] == "/base/worktrees/myproject/claude"
    assert result["codex"] == "/base/worktrees/myproject/codex"


# U-SE03: write_instruction_files — instructions.write() 호출됨 (mock)
def test_u_se03_write_instruction_files():
    worktrees = {"claude": "/base/myproject/claude", "codex": "/base/myproject/codex"}
    with patch("agent_crew.setup.instructions") as mock_instr:
        write_instruction_files(worktrees, project="myproject", port_file="/base/port")
    assert mock_instr.write.call_count == 2


# U-SE04: write_sessions_json — cmd 필드 포함 sessions.json 저장
def test_u_se04_write_sessions_json(tmp_path):
    agents = [
        {"name": "claude", "pane": 1},
        {"name": "codex", "pane": 2},
    ]
    path = str(tmp_path / "sessions.json")
    with patch("agent_crew.setup.session") as mock_session:
        write_sessions_json(path, agents)
    mock_session.save_sessions.assert_called_once()
    saved_agents = mock_session.save_sessions.call_args[0][1]
    for agent in saved_agents:
        assert "cmd" in agent


# U-SE05: find_free_port — 8100 bind fails (OS rejects it), returns 8101
def test_u_se05_find_free_port():
    mock_sock = MagicMock()
    mock_sock.__enter__ = lambda s: s
    mock_sock.__exit__ = MagicMock(return_value=False)
    mock_sock.bind.side_effect = lambda addr: (_ for _ in ()).throw(OSError()) if addr[1] == 8100 else None

    with patch("agent_crew.setup._collect_active_ports", return_value=set()), \
         patch("agent_crew.setup.socket.socket", return_value=mock_sock):
        port = find_free_port(start=8100)
    assert port == 8101


# U-SE06: write_port_file — 포트 번호 파일에 저장
def test_u_se06_write_port_file(tmp_path):
    path = str(tmp_path / "port")
    write_port_file(path, 8102)
    assert open(path).read().strip() == "8102"


# U-SE07: create_worktrees with custom agents — --agents 플래그 반영 (explicit project_path)
def test_u_se07_create_worktrees_custom_agents():
    agents = ["alpha", "beta", "gamma"]
    mock_result = MagicMock(returncode=0)
    def mock_isdir(path):
        # Return False to bypass backward compatibility (old paths don't exist)
        return False
    with patch("agent_crew.setup.subprocess.run", return_value=mock_result), \
         patch("agent_crew.setup.os.path.isdir", side_effect=mock_isdir):
        result = create_worktrees("proj", "/base", agents, project_path="/mock/project")
    assert set(result.keys()) == {"alpha", "beta", "gamma"}
    for agent in agents:
        assert result[agent] == f"/base/worktrees/proj/{agent}"


# U-SE08: start_agents_in_panes only launches the CLI — no kickoff prompt.
# Kickoff was removed because it consumed agent API quota and on rate-limited
# backends (codex) it stalled subsequent task pushes. Instruction files in the
# worktree cover the protocol.
def test_u_se08_start_agents_in_panes_uses_literal_send_keys():
    mock_result = MagicMock(returncode=0)
    with patch("agent_crew.setup.subprocess.run", return_value=mock_result) as mock_run, \
         patch("agent_crew.setup.time.sleep"):
        start_agents_in_panes("crew_proj", ["claude"])

    # 2 calls per agent (launch command + Enter); no kickoff prompt
    assert mock_run.call_count == 2
    launch_args = mock_run.call_args_list[0][0][0]
    enter_args = mock_run.call_args_list[1][0][0]
    # literal send for the launch command
    assert "-l" in launch_args
    assert "claude" in launch_args[-1]
    # Enter send to submit launch
    assert "Enter" in enter_args
    # Must NOT send any prompt text that references the AGENT_CREW TASK block
    for call in mock_run.call_args_list:
        args = call[0][0]
        last = args[-1] if args else ""
        assert "AGENT_CREW TASK" not in last
        assert "while true" not in last
        assert "mktemp" not in last


# U-SE09: _get_agent_cmd — claude omits --continue when .claude/projects/ absent
def test_u_se09_get_agent_cmd_claude_omits_continue_when_no_projects(tmp_path):
    cmd = _get_agent_cmd("claude", str(tmp_path))
    assert "--continue" not in cmd
    assert "--dangerously-skip-permissions" in cmd


# U-SE10: _get_agent_cmd — claude keeps --continue when .claude/projects/ present
def test_u_se10_get_agent_cmd_claude_keeps_continue_when_projects_exist(tmp_path):
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    cmd = _get_agent_cmd("claude", str(tmp_path))
    assert "--continue" in cmd


# U-SE11: start_agents_in_panes sends trust response "1" after codex startup
def test_u_se11_codex_trust_prompt_auto_answered():
    mock_result = MagicMock(returncode=0)
    with patch("agent_crew.setup.subprocess.run", return_value=mock_result) as mock_run, \
         patch("agent_crew.setup.time.sleep"):
        start_agents_in_panes("crew", ["codex"])

    all_texts = [
        call[0][0][-1]
        for call in mock_run.call_args_list
        if "-l" in call[0][0]
    ]
    assert "1" in all_texts


# U-SE12: pretrust_claude_worktree writes hasTrustDialogAccepted for claude worktree
# Claude's --dangerously-skip-permissions doesn't skip the workspace trust dialog,
# so first launch in a new worktree would stall. We pre-seed the flag in
# ~/.claude.json so the dialog is auto-accepted.
def test_u_se12_pretrust_claude_worktree(tmp_path):
    import json as _json
    config = tmp_path / ".claude.json"
    config.write_text(_json.dumps({"projects": {"/existing": {"hasTrustDialogAccepted": True}}}))

    wt = tmp_path / "wt_claude"
    wt.mkdir()
    with patch("agent_crew.setup.os.path.expanduser", return_value=str(config)):
        pretrust_claude_worktree({"claude": str(wt)})

    data = _json.loads(config.read_text())
    # Existing entries preserved.
    assert data["projects"]["/existing"]["hasTrustDialogAccepted"] is True
    # New worktree path registered as trusted.
    assert data["projects"][str(wt)]["hasTrustDialogAccepted"] is True


# U-SE13: pretrust_claude_worktree — no-op when ~/.claude.json missing
# Fresh install: Claude will create the file on first launch; don't synthesize one.
def test_u_se13_pretrust_noop_when_config_missing(tmp_path):
    missing = tmp_path / "nope.json"
    wt = tmp_path / "wt_claude"
    wt.mkdir()
    with patch("agent_crew.setup.os.path.expanduser", return_value=str(missing)):
        pretrust_claude_worktree({"claude": str(wt)})
    assert not missing.exists()


# U-SE14: pretrust_claude_worktree — no-op when claude not in worktrees
def test_u_se14_pretrust_noop_when_no_claude_worktree(tmp_path):
    import json as _json
    config = tmp_path / ".claude.json"
    original = {"projects": {}}
    config.write_text(_json.dumps(original))
    with patch("agent_crew.setup.os.path.expanduser", return_value=str(config)):
        pretrust_claude_worktree({"codex": str(tmp_path / "wt_codex")})
    # File untouched.
    assert _json.loads(config.read_text()) == original


# U-SE15: find_free_port skips ports from active project port files
def test_u_se15_find_free_port_skips_active_port_files():
    """Port 8100 is referenced by an active server — must be skipped without bind attempt."""
    mock_sock = MagicMock()
    mock_sock.__enter__ = lambda s: s
    mock_sock.__exit__ = MagicMock(return_value=False)
    mock_sock.bind.side_effect = None  # 8101 binds fine

    with patch("agent_crew.setup._collect_active_ports", return_value={8100}), \
         patch("agent_crew.setup.socket.socket", return_value=mock_sock):
        port = find_free_port(start=8100)

    assert port == 8101
    called_ports = [call[0][0][1] for call in mock_sock.bind.call_args_list]
    assert 8100 not in called_ports


# U-SE16: find_free_port reuses port from dead project (stale port file, no server)
def test_u_se16_find_free_port_reuses_dead_project_port():
    """Port 8100 in a stale port file but server is dead — must be eligible."""
    mock_sock = MagicMock()
    mock_sock.__enter__ = lambda s: s
    mock_sock.__exit__ = MagicMock(return_value=False)
    mock_sock.bind.side_effect = None  # 8100 binds fine

    with patch("agent_crew.setup._collect_active_ports", return_value=set()), \
         patch("agent_crew.setup.socket.socket", return_value=mock_sock):
        port = find_free_port(start=8100)

    assert port == 8100


# U-SE17: _collect_active_ports scans all ~/.agent_crew/*/port files
def test_u_se17_collect_active_ports_scans_all_port_files(tmp_path):
    """Only ports whose servers are listening are returned."""
    (tmp_path / "proj_a").mkdir()
    (tmp_path / "proj_a" / "port").write_text("8100")
    (tmp_path / "proj_b").mkdir()
    (tmp_path / "proj_b" / "port").write_text("8101")
    (tmp_path / "proj_c").mkdir()  # no port file

    with patch("agent_crew.setup._is_port_listening", side_effect=lambda p: p == 8100):
        active = _collect_active_ports(base=str(tmp_path))

    assert active == {8100}
    assert 8101 not in active


# U-SE18: _collect_active_ports silently skips corrupt port files
def test_u_se18_collect_active_ports_handles_corrupt_port_files(tmp_path):
    """A port file with non-integer content is silently skipped."""
    (tmp_path / "bad_proj").mkdir()
    (tmp_path / "bad_proj" / "port").write_text("not-a-number")
    (tmp_path / "good_proj").mkdir()
    (tmp_path / "good_proj" / "port").write_text("8200")

    with patch("agent_crew.setup._is_port_listening", return_value=True):
        active = _collect_active_ports(base=str(tmp_path))

    assert 8200 in active  # no exception for bad_proj


# U-SE19: _is_port_listening returns True when a server is bound
def test_u_se19_is_port_listening_returns_true():
    import socket as real_socket
    server = real_socket.socket(real_socket.AF_INET, real_socket.SOCK_STREAM)
    server.setsockopt(real_socket.SOL_SOCKET, real_socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        assert _is_port_listening(port) is True
    finally:
        server.close()


# U-SE20: _is_port_listening returns False for an unbound port
def test_u_se20_is_port_listening_returns_false():
    import socket as real_socket
    s = real_socket.socket(real_socket.AF_INET, real_socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert _is_port_listening(port) is False
