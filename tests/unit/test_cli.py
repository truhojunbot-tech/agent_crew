from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from agent_crew.cli import _capture_pane, _pane_looks_idle, crew


# U-C01: crew --help → exit 0, 서브커맨드 목록 포함
def test_u_c01_crew_help():
    runner = CliRunner()
    result = runner.invoke(crew, ["--help"])
    assert result.exit_code == 0
    assert "setup" in result.output
    assert "status" in result.output
    assert "run" in result.output
    assert "discuss" in result.output
    assert "teardown" in result.output


# U-C02: crew setup (project 이름 없음) → 에러
def test_u_c02_crew_setup_no_project():
    runner = CliRunner()
    result = runner.invoke(crew, ["setup"])
    assert result.exit_code != 0


# U-C03: crew status <project> (존재하지 않는 프로젝트) → not found 에러, exit != 0
def test_u_c03_crew_status_no_project():
    runner = CliRunner()
    result = runner.invoke(crew, ["status", "nonexistent", "--base", "/tmp/_crew_no_such_base"])
    assert result.exit_code != 0
    assert len(result.output.strip()) > 0


# U-C04: crew run (빈 task) → 에러
def test_u_c04_crew_run_empty_task():
    runner = CliRunner()
    result = runner.invoke(crew, ["run", ""])
    assert result.exit_code != 0


# U-C05: crew discuss (빈 topic) → 에러
def test_u_c05_crew_discuss_empty_topic():
    runner = CliRunner()
    result = runner.invoke(crew, ["discuss", ""])
    assert result.exit_code != 0


# U-C06: crew teardown <project> (존재하지 않는 프로젝트) → not found 에러, exit != 0
def test_u_c06_crew_teardown():
    runner = CliRunner()
    result = runner.invoke(crew, ["teardown", "nonexistent", "--base", "/tmp/_crew_no_such_base"])
    assert result.exit_code != 0
    assert len(result.output.strip()) > 0


# U-C07: _pane_looks_idle — shell prompt detected
def test_u_c07_pane_looks_idle_shell_prompt():
    assert _pane_looks_idle("some output\n$ ") is True
    assert _pane_looks_idle("some output\n$") is True
    assert _pane_looks_idle("some output\n❯ ") is True
    assert _pane_looks_idle("some output\n>>> ") is True
    assert _pane_looks_idle("Completed task") is True


# U-C08: _pane_looks_idle — agent still working
def test_u_c08_pane_looks_idle_active():
    assert _pane_looks_idle("Running tests...\ntest_foo PASSED") is False
    assert _pane_looks_idle("Writing file src/main.py") is False
    assert _pane_looks_idle("") is False


# U-C09: _capture_pane — successful capture
def test_u_c09_capture_pane_success():
    mock_result = MagicMock(returncode=0, stdout="line1\nline2\n$ ")
    with patch("agent_crew.cli.subprocess.run", return_value=mock_result) as mock_run:
        output = _capture_pane("crew_test", 0)
    assert output == "line1\nline2\n$ "
    args = mock_run.call_args[0][0]
    assert "tmux" in args
    assert "capture-pane" in args


# U-C10: _capture_pane — tmux not available
def test_u_c10_capture_pane_failure():
    mock_result = MagicMock(returncode=1, stdout="")
    with patch("agent_crew.cli.subprocess.run", return_value=mock_result):
        output = _capture_pane("crew_test", 0)
    assert output is None
