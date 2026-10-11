import json
from types import SimpleNamespace

import pytest

from scripts import premerge_gate as gate


HEAD = "a" * 40
OLD = "b" * 40
MAIN = "c" * 40


def _comment(body, number):
    return {"body": body, "id": number, "created_at": f"2026-01-01T00:00:{number:02d}Z"}


def test_selects_latest_approval_only_for_current_head():
    pr = {"number": 7, "head": {"sha": HEAD}}
    comments = [_comment(f"[agent_crew review] verdict: ✅ approve\n**Summary:** PR #7 at {OLD}: approve.", 1)]
    assert not gate.needs_gate(pr, comments)
    comments.append(_comment(f"[agent_crew review] verdict: 🔄 request_changes\n**Summary:** PR #7 at {HEAD}: request_changes.", 2))
    assert not gate.needs_gate(pr, comments)
    comments.append(_comment(f"[agent_crew review] verdict: ✅ approve\n**Summary:** PR #7 at {HEAD}: approve.", 3))
    assert gate.needs_gate(pr, comments)
    comments.append(_comment(f"[agent_crew premerge gate]\nHEAD {HEAD}\nMAIN {MAIN}\nEXIT 0", 4))
    assert not gate.needs_gate(pr, comments)


def test_review_summary_short_sha_matches_current_head_only():
    pr = {"number": 7, "head": {"sha": HEAD}}
    assert gate.needs_gate(pr, [_comment(
        f"[agent_crew review] verdict: ✅ approve\n**Summary:** PR #7 at {HEAD[:7]}: approve.", 1)])
    assert not gate.needs_gate(pr, [_comment(
        f"[agent_crew review] verdict: ✅ approve\n**Summary:** PR #7 at {OLD[:7]}: approve.", 1)])


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
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"number": 7, "head": {"sha": HEAD}}]), stderr="")
        if args[:2] == ["gh", "api"] and args[2].startswith("repos/truhojunbot-tech/agent_crew/issues/7/comments?"):
            return SimpleNamespace(returncode=0, stdout=json.dumps([_comment(f"[agent_crew review] verdict: ✅ approve\n**Summary:** PR #7 at {HEAD}: approve.", 1)]), stderr="")
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
