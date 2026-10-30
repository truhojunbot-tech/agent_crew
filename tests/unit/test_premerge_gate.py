import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts import premerge_gate as gate


HEAD = "a" * 40
OLD = "b" * 40
MAIN = "c" * 40


def _comment(body, number):
    return {"body": body, "id": number, "created_at": f"2026-01-01T00:00:{number:02d}Z",
            "author_association": "OWNER", "user": {"login": "truhojunbot-tech"}}


def _pr(number, sha, repo=gate.REPO):
    return {"number": number, "head": {"sha": sha, "repo": {"full_name": repo}}}


def test_untrusted_comments_and_fork_heads_cannot_trigger_or_suppress_gate():
    pr = _pr(7, HEAD)
    approval = _approved(7, HEAD)
    forged = dict(approval, author_association="NONE", user={"login": "stranger"})
    assert not gate.needs_gate(pr, [forged])
    assert gate.needs_gate(pr, [approval])
    fake_gate = _comment(f"{gate.MARKER}\nHEAD {HEAD}\nMAIN {MAIN}\nEXIT 0", 2)
    fake_gate.update(author_association="NONE", user={"login": "stranger"})
    assert gate.needs_gate(pr, [approval, fake_gate])
    assert gate.gate_for_head([fake_gate], HEAD) is None
    fake_legacy = _comment(f"**Coordinator merge gate:** full suite green on PR head {HEAD}", 4)
    fake_legacy.update(author_association="NONE", user={"login": "stranger"})
    assert gate.needs_gate(pr, [approval, fake_legacy])
    assert not gate.legacy_gate_for_head([fake_legacy], HEAD)
    fake_ack = _comment(f"{gate.BLACKBOARD_MARKER}\nHEAD {HEAD}\nposted", 3)
    fake_ack.update(author_association="NONE", user={"login": "stranger"})
    assert not gate.blackboard_posted([fake_ack], HEAD)
    assert not gate.needs_gate(_pr(7, HEAD, repo="stranger/fork"), [approval])


def test_main_skips_fork_before_reading_comments_or_fetching_code(tmp_path, monkeypatch):
    def api(endpoint):
        if "/pulls?" in endpoint:
            return [_pr(7, HEAD, repo="stranger/fork")]
        pytest.fail("fork comments were fetched")
    monkeypatch.setattr(gate, "_api_list", api)
    monkeypatch.setattr(gate, "checked", lambda *_args, **_kwargs:
                        pytest.fail("fork code was fetched"))
    assert gate.main(["--lock-file", str(tmp_path / "lock")]) == 0


def test_stale_structured_approval_is_unbound(tmp_path, monkeypatch, capsys):
    pr = _pr(7, HEAD)
    comments = [_approved(7, OLD)]
    assert not gate.needs_gate(pr, comments)
    assert gate.unbound_approval(pr, comments)
    monkeypatch.setattr(gate, "_api_list", lambda endpoint:
                        [pr] if "/pulls?" in endpoint else comments)
    assert gate.main(["--lock-file", str(tmp_path / "lock")]) == 1
    assert "approval_unbound" in capsys.readouterr().err


def test_selects_latest_approval_only_for_current_head():
    pr = _pr(7, HEAD)
    comments = [_approved(7, OLD)]
    assert not gate.needs_gate(pr, comments)
    comments.append(_comment(f"[agent_crew review] verdict: 🔄 request_changes\n**Summary:** PR #7 at {HEAD}: request_changes.", 2))
    assert not gate.needs_gate(pr, comments)
    comments.append({**_approved(7, HEAD), "id": 3, "created_at": "2026-01-01T00:00:03Z"})
    assert gate.needs_gate(pr, comments)
    comments.append(_comment(f"[agent_crew premerge gate]\nHEAD {HEAD}\nMAIN {MAIN}\nEXIT 0", 4))
    assert not gate.needs_gate(pr, comments)


def test_review_summary_short_sha_matches_current_head_only():
    pr = _pr(7, HEAD)
    assert not gate.needs_gate(pr, [_comment(
        f"[agent_crew review] verdict: ✅ approve\n**Summary:** PR #7 at {HEAD[:7]}: approve.", 1)])
    assert not gate.needs_gate(pr, [_comment(
        f"[agent_crew review] verdict: ✅ approve\n**Summary:** PR #7 at {OLD[:7]}: approve.", 1)])


def test_structured_review_head_binds_independent_of_summary():
    pr = _pr(7, HEAD)
    assert gate.needs_gate(pr, [_comment(
        f"[agent_crew review] verdict: ✅ approve\n\n**Summary:** PR #7 @ {OLD[:7]} approve\n\n"
        f"> reviewed head: {HEAD}\n> reviewer: codex | task: review-7", 1)])
    assert gate.needs_gate(pr, [_comment(
        f"[agent_crew review] verdict: ✅ approve\n\n**Summary:** approved\n\n"
        f"> reviewed head: {HEAD}\n> reviewer: codex | task: review-7", 1)])
    assert not gate.needs_gate(pr, [_comment(
        f"[agent_crew review] verdict: ✅ approve\n\n**Summary:** approved\n\n"
        f"> reviewed head: {OLD}\n> reviewer: codex | task: review-7", 1)])


@pytest.mark.parametrize("structured", [False, True])
def test_unbound_approval_posts_one_blocker_per_head(tmp_path, monkeypatch, structured):
    pr = _pr(7, HEAD)
    comments = [_comment(
        f"[agent_crew review] verdict: ✅ approve\n**Summary:** PR #7 at {HEAD[:7]} approve"
        + (f"\n> reviewed head: {OLD}\n> reviewer: codex | task: review-7"
           if structured else ""), 1)]
    monkeypatch.setattr(gate, "_api_list", lambda endpoint:
                        [pr] if "/pulls?" in endpoint else comments)
    delivered = []
    monkeypatch.setattr(gate, "post_blackboard", lambda _command, payload:
                        delivered.append(payload))

    def checked(args, **_kwargs):
        if args[:3] == ["gh", "pr", "comment"]:
            comments.append(_comment(args[args.index("--body") + 1], len(comments) + 1))
        return ""

    monkeypatch.setattr(gate, "checked", checked)
    opts = ["--lock-file", str(tmp_path / "lock"), "--blackboard-command", "bb post"]
    assert gate.main(opts) == 1
    assert delivered == [{"repo": gate.REPO, "pr_number": 7,
                          "head_sha": HEAD, "reason": "approval_unbound"}]
    assert gate.main(opts) == 1
    assert len(delivered) == 1


def test_lock_refuses_second_run(tmp_path):
    lock = tmp_path / "gate.lock"
    with gate.single_run_lock(lock):
        with pytest.raises(gate.AlreadyRunning):
            with gate.single_run_lock(lock):
                pass
    with gate.single_run_lock(lock):
        pass


def test_comment_has_shas_summary_and_exit():
    body = gate.gate_comment(HEAD, MAIN, "4555 passed, 19 skipped, 38 xfailed in 636s", 0)
    assert "[agent_crew premerge gate]" in body
    assert f"HEAD {HEAD}" in body
    assert f"MAIN {MAIN}" in body
    assert "4555 passed, 19 skipped, 38 xfailed" in body
    assert "EXIT 0" in body


def test_configured_blackboard_command_receives_result_json(monkeypatch):
    calls = []
    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(gate, "run", fake_run)
    payload = {"pr_number": 7, "head_sha": HEAD, "exit_code": 1}
    gate.post_blackboard("blackboard post-result", payload)
    assert calls[0][0] == ["blackboard", "post-result"]
    assert json.loads(calls[0][1]["input"]) == payload


def test_run_gate_uses_temporary_worktree_and_cleans_up(tmp_path, monkeypatch):
    calls = []
    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ["gh", "api"] and args[2].startswith("repos/truhojunbot-tech/agent_crew/pulls?"):
            return SimpleNamespace(returncode=0, stdout=json.dumps([_pr(7, HEAD)]), stderr="")
        if args[:2] == ["gh", "api"] and args[2].startswith("repos/truhojunbot-tech/agent_crew/issues/7/comments?"):
            return SimpleNamespace(returncode=0, stdout=json.dumps([_approved(7, HEAD)]), stderr="")
        if args == ["git", "rev-parse", "origin/main"]:
            return SimpleNamespace(returncode=0, stdout=MAIN + "\n", stderr="")
        if args == ["git", "rev-parse", "FETCH_HEAD"]:
            return SimpleNamespace(returncode=0, stdout=HEAD + "\n", stderr="")
        if "pytest" in args:
            return SimpleNamespace(returncode=0, stdout="4555 passed, 19 skipped in 600s\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(gate, "run", fake_run)
    monkeypatch.setattr(gate, "safe_rmtree", lambda path: calls.append((["rmtree", str(path)], {})))
    gate.main(["--lock-file", str(tmp_path / "lock"), "--work-dir", str(tmp_path)])
    assert any(args[:4] == ["git", "worktree", "add", "--detach"] for args, _ in calls)
    assert any(args[:3] == ["git", "worktree", "remove"] for args, _ in calls)
    assert any(args[:3] == ["gh", "pr", "comment"] and "EXIT 0" in args[args.index("--body") + 1] for args, _ in calls)


def _approved(number, sha):
    return _comment(f"[agent_crew review] verdict: ✅ approve\n\n"
                    f"**Summary:** PR #{number} at {sha[:7]}: approve.\n\n"
                    f"> reviewed head: {sha}\n> reviewer: codex | task: review-{number}", 1)


def test_one_pr_error_does_not_block_later_approved_pr(tmp_path, monkeypatch, capsys):
    prs = [_pr(7, HEAD), _pr(8, OLD)]
    monkeypatch.setattr(gate, "_api_list", lambda endpoint: prs if "/pulls?" in endpoint else
                        [_approved(int(endpoint.split("/issues/")[1].split("/")[0]),
                                   HEAD if "/issues/7/" in endpoint else OLD)])
    posted = []
    fetched = []
    def checked(args, **_kwargs):
        if args[:4] == ["git", "fetch", "origin", "pull/7/head"]:
            fetched.append(HEAD)
        if args[:4] == ["git", "fetch", "origin", "pull/8/head"]:
            fetched.append(OLD)
        if args[:3] == ["git", "rev-parse", "origin/main"]:
            return MAIN
        if args[:3] == ["git", "rev-parse", "FETCH_HEAD"]:
            return fetched[-1]
        if args[:3] == ["gh", "pr", "comment"]:
            posted.append(int(args[3]))
        return ""
    monkeypatch.setattr(gate, "checked", checked)
    def test_pr(number, *_args):
        if number == 7:
            raise RuntimeError("worktree add failed")
        return "1 passed in 1s", 0
    monkeypatch.setattr(gate, "test_pr", test_pr)
    assert gate.main(["--lock-file", str(tmp_path / "lock")]) == 1
    assert posted == [8]
    assert "PR #7" in capsys.readouterr().err


def test_unbound_approved_review_is_logged(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gate, "_api_list", lambda endpoint:
                        [_pr(7, HEAD)] if "/pulls?" in endpoint else
                        [_comment("[agent_crew review] verdict: ✅ approve\n"
                                  f"**Summary:** reviewed_sha {HEAD}", 1)])
    assert gate.main(["--lock-file", str(tmp_path / "lock")]) == 1
    assert "PR #7" in capsys.readouterr().err


def test_existing_coordinator_gate_is_not_reported_as_unbound(tmp_path, monkeypatch, capsys):
    comments = [_approved(7, HEAD),
                _comment(f"**Coordinator merge gate:** full suite green on PR head {HEAD}", 2)]
    monkeypatch.setattr(gate, "_api_list", lambda endpoint:
                        [_pr(7, HEAD)] if "/pulls?" in endpoint else comments)
    assert gate.main(["--lock-file", str(tmp_path / "lock")]) == 0
    assert not capsys.readouterr().err


def test_blackboard_failure_allows_next_pr_and_retries_without_retesting(tmp_path, monkeypatch):
    prs = [_pr(7, HEAD), _pr(8, OLD)]
    comments = {7: [_approved(7, HEAD)], 8: [_approved(8, OLD)]}
    monkeypatch.setattr(gate, "_api_list", lambda endpoint: prs if "/pulls?" in endpoint else
                        comments[int(endpoint.split("/issues/")[1].split("/")[0])])
    fetched = []
    def checked(args, **_kwargs):
        if args[:3] == ["git", "fetch", "origin"] and args[3].startswith("pull/"):
            fetched.append(HEAD if args[3] == "pull/7/head" else OLD)
        if args == ["git", "rev-parse", "origin/main"]:
            return MAIN
        if args == ["git", "rev-parse", "FETCH_HEAD"]:
            return fetched[-1]
        if args[:3] == ["gh", "pr", "comment"]:
            number = int(args[3])
            comments[number].append(_comment(args[args.index("--body") + 1], len(comments[number]) + 1))
        return ""
    monkeypatch.setattr(gate, "checked", checked)
    tested = []
    monkeypatch.setattr(gate, "test_pr", lambda number, *_args:
                        (tested.append(number) or "1 passed in 1s", 0))
    posted = []
    def blackboard(_command, payload):
        posted.append(payload["pr_number"])
        if posted == [7]:
            raise RuntimeError("blackboard unavailable")
    monkeypatch.setattr(gate, "post_blackboard", blackboard)
    opts = ["--lock-file", str(tmp_path / "lock"), "--blackboard-command", "bb post"]
    assert gate.main(opts) == 1
    assert tested == [7, 8]
    assert any(gate.MARKER in c["body"] for c in comments[7])
    assert not any(gate.BLACKBOARD_MARKER in c["body"] for c in comments[7])
    assert any(gate.BLACKBOARD_MARKER in c["body"] for c in comments[8])
    assert gate.main(opts) == 0
    assert tested == [7, 8]
    assert posted == [7, 8, 7]
    assert any(gate.BLACKBOARD_MARKER in c["body"] for c in comments[7])


def test_head_moved_is_skipped_with_failure_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gate, "_api_list", lambda endpoint:
                        [_pr(7, HEAD)] if "/pulls?" in endpoint else
                        [_approved(7, HEAD)])
    monkeypatch.setattr(gate, "checked", lambda args, **_kwargs:
                        MAIN if args == ["git", "rev-parse", "origin/main"] else OLD)
    monkeypatch.setattr(gate, "test_pr", lambda *_args: pytest.fail("moved head was tested"))
    assert gate.main(["--lock-file", str(tmp_path / "lock")]) == 1
    assert "head changed" in capsys.readouterr().err


@pytest.mark.parametrize("summary,exit_code", [
    ("1 failed, 2 passed in 1s", 1),
    ("pytest not run: squash conflict", 1),
])
def test_nonzero_gate_result_is_commented(tmp_path, monkeypatch, summary, exit_code):
    monkeypatch.setattr(gate, "_api_list", lambda endpoint:
                        [_pr(7, HEAD)] if "/pulls?" in endpoint else
                        [_approved(7, HEAD)])
    bodies = []
    def checked(args, **_kwargs):
        if args == ["git", "rev-parse", "origin/main"]:
            return MAIN
        if args == ["git", "rev-parse", "FETCH_HEAD"]:
            return HEAD
        if args[:3] == ["gh", "pr", "comment"]:
            bodies.append(args[args.index("--body") + 1])
        return ""
    monkeypatch.setattr(gate, "checked", checked)
    monkeypatch.setattr(gate, "test_pr", lambda *_args: (summary, exit_code))
    assert gate.main(["--lock-file", str(tmp_path / "lock")]) == 0
    assert len(bodies) == 1
    assert f"pytest: {summary}" in bodies[0]
    assert f"EXIT {exit_code}" in bodies[0]


def test_main_exits_cleanly_if_lock_is_held(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "_api_list", lambda *_args: pytest.fail("lock did not hold"))
    lock = tmp_path / "lock"
    with gate.single_run_lock(lock):
        assert gate.main(["--lock-file", str(lock)]) == 0


@pytest.mark.parametrize("outcome,expected_summary,expected_exit", [
    ("failed", "1 failed, 2 passed in 1s", 1),
    ("conflict", "pytest not run: squash conflict", 1),
    ("timeout", "pytest timed out after 7200s", 124),
    ("error", None, None),
])
def test_pr_cleans_worktree_and_basetemp_on_all_outcomes(
        tmp_path, monkeypatch, outcome, expected_summary, expected_exit):
    monkeypatch.setattr(gate, "BASETEMP_ROOT", tmp_path)
    basetemp = tmp_path / "ac_gate_777"
    basetemp.mkdir()
    (basetemp / "stale").write_text("old")
    commands = []
    def fake_run(args, **_kwargs):
        commands.append(args)
        if args[:2] == ["git", "worktree"] and args[2] == "add":
            assert not basetemp.exists()
        if "--squash" in args and outcome == "conflict":
            return SimpleNamespace(returncode=1, stdout="", stderr="conflict")
        if "pytest" in args:
            basetemp.mkdir()
            (basetemp / "created").write_text("new")
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(args, 7300)
            if outcome == "error":
                raise RuntimeError("pytest launcher error")
            return SimpleNamespace(returncode=1, stdout="1 failed, 2 passed in 1s\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(gate, "run", fake_run)
    if outcome == "error":
        with pytest.raises(RuntimeError, match="pytest launcher error"):
            gate.test_pr(777, HEAD, MAIN, tmp_path)
    else:
        assert gate.test_pr(777, HEAD, MAIN, tmp_path) == (expected_summary, expected_exit)
    assert not basetemp.exists()
    assert any(args[:3] == ["git", "worktree", "remove"] for args in commands)
    assert not list(tmp_path.glob("ac_gate_777_*/"))


def test_pytest_summary_extracts_decorated_line():
    assert gate.pytest_summary("failures above\n=== 2 failed, 3 passed, 1 skipped in 9s ===\n") == \
           "2 failed, 3 passed, 1 skipped in 9s"


def test_pytest_summary_names_up_to_three_failures_in_posted_summary():
    output = "\n".join([
        "FAILED tests/unit/test_a.py::test_one - AssertionError",
        "ERROR tests/unit/test_b.py::test_two - RuntimeError",
        "FAILED tests/unit/test_c.py::test_three - AssertionError",
        "FAILED tests/unit/test_d.py::test_four - AssertionError",
        "=== 3 failed, 1 error, 10 passed in 9s ===",
    ])
    summary = gate.pytest_summary(output)
    assert "tests/unit/test_a.py::test_one" in summary
    assert "tests/unit/test_b.py::test_two" in summary
    assert "tests/unit/test_c.py::test_three" in summary
    assert "test_four" not in summary
    assert len(summary) <= 300
    assert gate.gate_payload(7, gate.gate_comment(HEAD, MAIN, summary, 1))["summary"] == summary


def test_pytest_summary_truncates_long_failure_nodes():
    output = ("FAILED tests/unit/test_long.py::test_" + "x" * 400 +
              " - AssertionError\n=== 1 failed in 1s ===\n")
    assert len(gate.pytest_summary(output)) == 300
