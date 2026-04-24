import json
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from agent_crew.triage import build_prompt, filter_processed, parse_issues, parse_response


def make_issue(number=1, title="Fix bug", labels=None):
    label_objs = [{"name": label} for label in (labels or [])]
    return {"number": number, "title": title, "labels": label_objs, "body": "details"}


# U-T01: Parse GitHub issues JSON — number, title, labels correctly extracted
def test_u_t01_parse_issues():
    raw = [
        make_issue(1, "Add feature", ["enhancement", "agent_crew:todo"]),
        make_issue(2, "Fix bug", ["bug"]),
    ]
    result = parse_issues(raw)
    assert len(result) == 2
    assert result[0]["number"] == 1
    assert result[0]["title"] == "Add feature"
    assert result[0]["labels"] == ["enhancement", "agent_crew:todo"]
    assert result[1]["number"] == 2
    assert result[1]["labels"] == ["bug"]


# U-T02: Filter already-processed issues — "agent_crew:done" excluded
def test_u_t02_filter_processed():
    issues = [
        {"number": 1, "title": "Open", "labels": ["enhancement"]},
        {"number": 2, "title": "Done", "labels": ["agent_crew:done"]},
        {"number": 3, "title": "Also open", "labels": []},
    ]
    result = filter_processed(issues)
    assert len(result) == 2
    assert all(i["number"] != 2 for i in result)


# U-T03: Build triage prompt — issues and merge history present
def test_u_t03_build_prompt():
    issues = [
        {"number": 1, "title": "Add queue", "labels": ["enhancement"]},
        {"number": 2, "title": "Fix crash", "labels": ["bug"]},
    ]
    history = "Merged PR #10: implement protocol.py"
    prompt = build_prompt(issues, history)
    assert "Add queue" in prompt
    assert "Fix crash" in prompt
    assert history in prompt
    assert len(prompt) > 0


# U-T04: Parse triage agent response — issue number + description extracted
def test_u_t04_parse_response():
    text = "ISSUE: 3\nDESCRIPTION: Implement session manager with health check"
    result = parse_response(text)
    assert result is not None
    assert result["issue"] == 3
    assert "session" in result["description"].lower()


# U-T05: No open issues → build_prompt returns None
def test_u_t05_empty_issues():
    assert build_prompt([], "some history") is None


# U-T04b: parse_response ignores ISSUE:/DESCRIPTION: embedded in prose
def test_u_t04b_parse_response_anchored():
    # inline prose — should NOT match because not at line start
    prose = "We might tackle ISSUE: 99 later. The DESCRIPTION: unclear."
    result = parse_response(prose)
    assert result is None


# U-T04c: parse_response handles malformed response → None
def test_u_t04c_parse_response_missing_fields():
    assert parse_response("just some text with no markers") is None
    assert parse_response("ISSUE: 5\n(no description line)") is None


# U-T06: get_project_git_origin — returns git remote URL from project dir
def test_u_t06_get_project_git_origin(tmp_path):
    """get_project_git_origin reads the 'origin' remote URL from a git repo."""
    import subprocess as sp
    from agent_crew.triage import get_project_git_origin

    # Init a git repo with a remote
    sp.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    sp.run(["git", "remote", "add", "origin", "https://github.com/org/myrepo.git"],
           cwd=str(tmp_path), capture_output=True)

    url = get_project_git_origin(str(tmp_path))
    assert url is not None
    assert "myrepo" in url


# U-T07: get_project_git_origin — returns None when no remote configured
def test_u_t07_get_project_git_origin_no_remote(tmp_path):
    """Returns None when no 'origin' remote is configured."""
    import subprocess as sp
    from agent_crew.triage import get_project_git_origin

    sp.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    url = get_project_git_origin(str(tmp_path))
    assert url is None


# U-T08: validate_repo_origin — mismatch gives (False, error_message)
def test_u_t08_validate_repo_origin_mismatch(tmp_path):
    """When --repo doesn't match the project's git remote, return (False, msg)
    with an error message naming both values."""
    import subprocess as sp
    from agent_crew.triage import validate_repo_origin

    sp.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    sp.run(["git", "remote", "add", "origin", "https://github.com/org/actual-repo.git"],
           cwd=str(tmp_path), capture_output=True)

    ok, msg = validate_repo_origin("org/other-repo", str(tmp_path))
    assert ok is False
    assert "other-repo" in msg or "mismatch" in msg.lower()
    assert "actual-repo" in msg or "org/other-repo" in msg


# U-T09: validate_repo_origin — match gives (True, "")
def test_u_t09_validate_repo_origin_match(tmp_path):
    """When --repo matches the project's git remote, return (True, '')."""
    import subprocess as sp
    from agent_crew.triage import validate_repo_origin

    sp.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    sp.run(["git", "remote", "add", "origin", "https://github.com/org/myrepo.git"],
           cwd=str(tmp_path), capture_output=True)

    # repo arg can be short form (owner/name) — should match the remote URL
    ok, msg = validate_repo_origin("org/myrepo", str(tmp_path))
    assert ok is True
    assert msg == ""


# U-T10: crew triage exits with repo mismatch error when --repo doesn't match project origin
def test_u_t10_triage_cli_exits_on_repo_mismatch(tmp_path):
    """crew triage --project X --repo Y must fail immediately with a clear error
    when Y does not match X's git origin URL. This prevents enqueueing issues
    from a different repo into the wrong project queue."""
    from agent_crew.cli import crew
    from agent_crew.queue import TaskQueue

    db_file = str(tmp_path / "tasks.db")
    TaskQueue(db_file)
    state = {
        "project": "proj_t10",
        "port": 0,
        "db": db_file,
        "session": "crew_proj_t10",
        "agents": ["claude"],
        "pane_ids": ["%10"],
    }
    (tmp_path / "proj_t10").mkdir()
    (tmp_path / "proj_t10" / "state.json").write_text(json.dumps(state))

    runner = CliRunner()
    # validate_repo_origin returns mismatch
    with patch("agent_crew.triage.validate_repo_origin",
               return_value=(False, "repo mismatch: --repo org/wrong but project proj_t10 origin is org/actual")):
        result = runner.invoke(crew, [
            "triage",
            "--repo", "org/wrong",
            "--project", "proj_t10",
            "--base", str(tmp_path),
            "--no-confirm",
        ])

    assert result.exit_code != 0
    output = result.output.lower()
    assert "mismatch" in output or "wrong" in output or "repo" in output
