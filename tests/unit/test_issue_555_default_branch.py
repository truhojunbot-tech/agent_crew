"""Worktree prep uses the remote's default branch when none is declared (#555)."""

import subprocess

import pytest

from agent_crew.server import WorktreeTargetUnresolved, _prepare_worktree_for_task


def _git(*args):
    return subprocess.run(
        ["git", *map(str, args)], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _worktree(tmp_path, *, default="master", extra=()):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    worker = tmp_path / "worker"
    _git("init", "--bare", f"--initial-branch={default}", remote)
    _git("init", f"--initial-branch={default}", seed)
    _git("-C", seed, "config", "user.name", "Test User")
    _git("-C", seed, "config", "user.email", "test@example.invalid")
    (seed / "marker").write_text(default)
    _git("-C", seed, "add", "marker")
    _git("-C", seed, "commit", "-m", default)
    _git("-C", seed, "remote", "add", "origin", remote)
    _git("-C", seed, "push", "origin", default)
    commits = {default: _git("-C", seed, "rev-parse", "HEAD")}
    for branch in extra:
        _git("-C", seed, "checkout", "-b", branch)
        (seed / "marker").write_text(branch)
        _git("-C", seed, "commit", "-am", branch)
        _git("-C", seed, "push", "origin", branch)
        commits[branch] = _git("-C", seed, "rev-parse", "HEAD")
    _git("clone", remote, worker)
    return worker, commits


def _prepared_base(worker):
    return _prepare_worktree_for_task(
        str(worker), "impl-555", "fix/output", "implementer",
    )


def test_master_only_remote_without_origin_head_uses_master(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)
    worker, commits = _worktree(tmp_path)
    _git("-C", worker, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")

    assert _prepared_base(worker) == commits["master"]
    assert _git("-C", worker, "rev-parse", "HEAD") == commits["master"]


def test_origin_head_symbolic_ref_to_third_branch_wins(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)
    worker, commits = _worktree(tmp_path, default="trunk", extra=("main",))

    assert _prepared_base(worker) == commits["trunk"]


def test_no_remote_default_or_main_or_master_refuses(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)
    worker, _ = _worktree(tmp_path, default="trunk")
    _git("-C", worker, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")

    with pytest.raises(WorktreeTargetUnresolved, match="origin/HEAD.*origin/main.*origin/master") as exc:
        _prepared_base(worker)
    assert exc.value.reason == "worktree_base_unresolved"


def test_explicit_env_branch_wins_over_origin_head(tmp_path, monkeypatch):
    worker, commits = _worktree(tmp_path, default="trunk", extra=("master",))
    monkeypatch.setenv("AGENT_CREW_MAIN_BRANCH", "master")

    assert _prepared_base(worker) == commits["master"]


def test_context_base_branch_keeps_priority_over_env(tmp_path, monkeypatch):
    worker, commits = _worktree(tmp_path, default="trunk", extra=("master", "main"))
    monkeypatch.setenv("AGENT_CREW_MAIN_BRANCH", "master")

    assert _prepare_worktree_for_task(
        str(worker), "impl-555", "fix/output", "implementer",
        {"base_branch": "main"},
    ) == commits["main"]
