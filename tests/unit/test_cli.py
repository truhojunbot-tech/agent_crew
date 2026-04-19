from click.testing import CliRunner

from agent_crew.cli import crew


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


# U-C03: crew status (실행 중 프로젝트 없음) → 깔끔한 메시지, exit 0
def test_u_c03_crew_status_no_project():
    runner = CliRunner()
    result = runner.invoke(crew, ["status"])
    assert result.exit_code == 0
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


# U-C06: crew teardown → exit 0
def test_u_c06_crew_teardown():
    runner = CliRunner()
    result = runner.invoke(crew, ["teardown"])
    assert result.exit_code == 0
