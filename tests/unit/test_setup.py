from unittest.mock import MagicMock, patch

from agent_crew.setup import (
    create_worktrees,
    find_free_port,
    validate_git_repo,
    start_agents_in_panes,
    write_instruction_files,
    write_port_file,
    write_sessions_json,
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


# U-SE02: create_worktrees — 올바른 경로 dict 반환
def test_u_se02_create_worktrees():
    mock_result = MagicMock(returncode=0)
    with patch("agent_crew.setup.subprocess.run", return_value=mock_result):
        result = create_worktrees("myproject", "/base", ["claude", "codex"])
    assert set(result.keys()) == {"claude", "codex"}
    assert result["claude"] == "/base/myproject/claude"
    assert result["codex"] == "/base/myproject/codex"


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


# U-SE05: find_free_port — 8100부터 사용 가능한 포트 반환 (mock socket)
def test_u_se05_find_free_port():
    mock_sock = MagicMock()
    mock_sock.__enter__ = lambda s: s
    mock_sock.__exit__ = MagicMock(return_value=False)
    mock_sock.connect_ex.side_effect = lambda addr: 0 if addr[1] == 8100 else 1

    with patch("agent_crew.setup.socket.socket", return_value=mock_sock):
        port = find_free_port(start=8100)
    assert port == 8101


# U-SE06: write_port_file — 포트 번호 파일에 저장
def test_u_se06_write_port_file(tmp_path):
    path = str(tmp_path / "port")
    write_port_file(path, 8102)
    assert open(path).read().strip() == "8102"


# U-SE07: create_worktrees with custom agents — --agents 플래그 반영
def test_u_se07_create_worktrees_custom_agents():
    agents = ["alpha", "beta", "gamma"]
    mock_result = MagicMock(returncode=0)
    with patch("agent_crew.setup.subprocess.run", return_value=mock_result):
        result = create_worktrees("proj", "/base", agents)
    assert set(result.keys()) == {"alpha", "beta", "gamma"}
    for agent in agents:
        assert result[agent] == f"/base/proj/{agent}"


# U-SE08: start_agents_in_panes uses literal tmux input for cmd/prompt and sends Enter separately
def test_u_se08_start_agents_in_panes_uses_literal_send_keys():
    mock_result = MagicMock(returncode=0)
    with patch("agent_crew.setup.subprocess.run", return_value=mock_result) as mock_run, \
         patch("agent_crew.setup.time.sleep"):
        start_agents_in_panes("crew_proj", ["claude"], 8100)

    assert mock_run.call_count == 4
    first_args = mock_run.call_args_list[0][0][0]
    third_args = mock_run.call_args_list[2][0][0]
    assert "-l" in first_args
    assert "-l" in third_args
    prompt_text = third_args[-1]
    assert "/tasks/next?role=implementer" in prompt_text
    assert "curl" in prompt_text
