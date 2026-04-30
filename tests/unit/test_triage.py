import json
import re
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


# ---------------------------------------------------------------------------
# U-T11..U-T17 — dependency-aware selection (Issue #82)
# ---------------------------------------------------------------------------


from agent_crew.triage import (  # noqa: E402
    fetch_recent_merge_history,
    filter_blocked,
    parse_dependencies,
)


# U-T11: parse_dependencies extracts Parent + Phase from issue body
def test_u_t11_parse_dependencies_basic():
    body = "Parent: #54 | Phase 2\n\nImplement RegimeDetector consumer."
    deps = parse_dependencies(body)
    assert deps["parents"] == [54]
    assert deps["phase"] == 2


def test_u_t11b_parse_dependencies_alternate_keywords():
    """`depends on` and `blocked by` should both register parents."""
    body = "Depends on #10 and blocked by #11. Phase 3."
    deps = parse_dependencies(body)
    assert sorted(deps["parents"]) == [10, 11]
    assert deps["phase"] == 3


def test_u_t11c_parse_dependencies_no_markers():
    deps = parse_dependencies("Plain description, no metadata.")
    assert deps["parents"] == []
    assert deps["phase"] is None


def test_u_t11d_parse_dependencies_none_body():
    deps = parse_dependencies(None)
    assert deps == {"parents": [], "phase": None}


def test_u_t11e_parse_dependencies_dedupes():
    body = "Parent: #54. Also Parent: #54. Depends on #54."
    deps = parse_dependencies(body)
    assert deps["parents"] == [54]


# U-T12: parse_issues now propagates parents + phase fields
def test_u_t12_parse_issues_carries_dependencies():
    raw = [
        {
            "number": 100,
            "title": "Build consumer",
            "labels": [],
            "body": "Parent: #54 | Phase 2",
        }
    ]
    result = parse_issues(raw)
    assert result[0]["parents"] == [54]
    assert result[0]["phase"] == 2


# U-T13: filter_blocked drops issues with open parents
def test_u_t13_filter_blocked_drops_unmet_parent():
    issues = [
        {"number": 54, "title": "Foundation", "labels": [], "parents": [], "phase": 1},
        {"number": 100, "title": "Consumer", "labels": [], "parents": [54], "phase": 2},
    ]
    eligible = filter_blocked(issues, closed_issue_numbers=set())
    # #54 has no parents → eligible. #100 depends on #54 (open) → blocked.
    assert {i["number"] for i in eligible} == {54}


def test_u_t13b_filter_blocked_unblocks_when_parent_closed():
    issues = [
        {"number": 100, "title": "Consumer", "labels": [], "parents": [54], "phase": 2},
    ]
    # #54 is in the closed set — #100 is now eligible.
    eligible = filter_blocked(issues, closed_issue_numbers={54})
    assert {i["number"] for i in eligible} == {100}


def test_u_t13c_filter_blocked_unblocks_when_parent_not_in_open_set():
    # If a parent is neither in `closed` nor in the open list, treat it as
    # already done (e.g. user closed-as-not-planned, or it was never an
    # actual issue — happens with cross-repo refs).
    issues = [
        {"number": 100, "title": "Consumer", "labels": [], "parents": [9999], "phase": 2},
    ]
    eligible = filter_blocked(issues, closed_issue_numbers=set())
    assert {i["number"] for i in eligible} == {100}


# U-T14: build_prompt surfaces phase + parents to the agent
def test_u_t14_build_prompt_shows_dependencies():
    issues = [
        {
            "number": 54, "title": "Build foundation",
            "labels": ["enhancement"], "parents": [], "phase": 1,
        },
        {
            "number": 100, "title": "Wire consumer",
            "labels": [], "parents": [54], "phase": 2,
        },
    ]
    prompt = build_prompt(issues, "none")
    assert "phase: 1" in prompt
    assert "phase: 2" in prompt
    assert "parents: #54" in prompt
    # The instruction must explicitly mention dependency-aware selection
    assert "dependencies" in prompt.lower() or "phase" in prompt.lower()


# U-T15: fetch_recent_merge_history returns "none" on subprocess failure
def test_u_t15_merge_history_subprocess_failure():
    with patch(
        "agent_crew.triage.subprocess.run",
        side_effect=Exception("gh missing"),
    ):
        assert fetch_recent_merge_history("any/repo") == "none"


def test_u_t15b_merge_history_empty_returns_none():
    fake = MagicMock(stdout="[]", returncode=0)
    with patch("agent_crew.triage.subprocess.run", return_value=fake):
        assert fetch_recent_merge_history("any/repo") == "none"


def test_u_t15c_merge_history_formats_pr_lines():
    fake = MagicMock(
        stdout=json.dumps(
            [
                {"number": 11, "title": "feat: A"},
                {"number": 12, "title": "fix: B"},
            ]
        ),
        returncode=0,
    )
    with patch("agent_crew.triage.subprocess.run", return_value=fake):
        history = fetch_recent_merge_history("any/repo")
    assert "#11" in history and "feat: A" in history
    assert "#12" in history and "fix: B" in history


# U-T16: severity pre-sort — blocker beats nice-to-have (#131)
def test_u_t16_severity_score_tiers():
    from agent_crew.triage import _severity_score
    assert _severity_score(["blocker", "bug"]) < _severity_score(["enhancement"])
    assert _severity_score(["blocked"]) < _severity_score(["p1"])
    assert _severity_score(["critical"]) < _severity_score(["high"])
    assert _severity_score(["p0"]) < _severity_score([])
    assert _severity_score(["p1"]) < _severity_score(["nice-to-have"])
    # Same tier → same score
    assert _severity_score(["blocker"]) == _severity_score(["p0"])


def test_u_t17_triage_run_picks_blocker_over_nice_to_have(tmp_db):
    """run() must hand only the top-severity tier to the LLM (#131)."""
    from unittest.mock import MagicMock, patch
    from agent_crew.triage import run
    from agent_crew.queue import TaskQueue

    queue = TaskQueue(tmp_db)

    blocker_issues = [
        {"number": 875, "title": "Blocker 1", "labels": [{"name": "bug"}, {"name": "blocked"}], "body": ""},
        {"number": 876, "title": "Blocker 2", "labels": [{"name": "blocker"}], "body": ""},
    ]
    nice_issues = [
        {"number": 888, "title": "Nice-to-have: code hygiene", "labels": [{"name": "enhancement"}], "body": ""},
    ]
    all_issues = blocker_issues + nice_issues

    seen_prompts: list[str] = []

    def agent_fn(prompt: str) -> str:
        seen_prompts.append(prompt)
        # Always pick the first issue listed in the prompt
        m = re.search(r"#(\d+):", prompt)
        num = m.group(1) if m else "875"
        return f"ISSUE: {num}\nDESCRIPTION: fix blocker"

    with (
        patch("agent_crew.triage.fetch_issues_from_gh", return_value=all_issues),
        patch("agent_crew.triage.fetch_closed_issue_numbers", return_value=set()),
        patch("agent_crew.triage.fetch_recent_merge_history", return_value="none"),
    ):
        result = run(queue, "org/repo", agent_fn, branch="main")

    assert result is not None
    # The prompt must NOT mention the nice-to-have issue
    assert len(seen_prompts) == 1
    assert "#888" not in seen_prompts[0], "nice-to-have leaked into blocker-tier prompt"
    assert "#875" in seen_prompts[0] or "#876" in seen_prompts[0]
