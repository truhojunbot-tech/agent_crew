"""Worktree prep uses the remote's default branch when none is declared (#555)."""

import subprocess

import pytest
from click.testing import CliRunner

from agent_crew.cli import _sync_worktrees_to_main, crew
from agent_crew.protocol import TaskResult
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


def test_existing_crew_run_branch_needs_no_default_base(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)
    worker, commits = _worktree(tmp_path, default="trunk")
    _git("-C", worker, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")

    assert _prepare_worktree_for_task(
        str(worker), "impl-555", "trunk", "implementer",
        {"crew_run_branch": True},
    ) == commits["trunk"]


def test_unborn_head_heals_from_remote_default_not_provisional_main(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)
    worker, commits = _worktree(tmp_path, default="trunk", extra=("main",))
    _git("-C", worker, "symbolic-ref", "HEAD", "refs/heads/fix/output")

    assert _prepare_worktree_for_task(
        str(worker), "impl-555", "fix/output", "implementer",
    ) == commits["trunk"]
    assert _git("-C", worker, "rev-parse", "refs/heads/fix/output") == commits["trunk"]


def test_cli_sync_falls_back_to_master_without_origin_head(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)
    worker, commits = _worktree(tmp_path)
    _git("-C", worker, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")

    landed = _sync_worktrees_to_main({"codex": str(worker)}, base_branch="main")

    assert landed["codex"] == {
        "requested_ref": "origin/main",
        "actual_ref": "origin/master",
        "sha": commits["master"],
        "status": "fallback",
    }
    assert _git("-C", worker, "rev-parse", "HEAD") == commits["master"]


def test_crew_run_records_master_as_implicit_base(tmp_path, monkeypatch):
    import agent_crew.cli as cli
    import agent_crew.loop as loop

    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)
    worker, commits = _worktree(tmp_path)
    _git("-C", worker, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    monkeypatch.setattr(cli, "_read_state", lambda *_args: {
        "port": 0, "pane_ids": [], "worktrees": {"codex": str(worker)},
    })

    class Queue:
        def list_tasks(self):
            return []

        def get_result(self, task_id):
            return TaskResult(task_id, "failed", "fixture stops after enqueue")

    monkeypatch.setattr(cli, "_writable_queue", lambda *_args, **_kwargs: Queue())
    captured = []
    monkeypatch.setattr(loop, "enqueue_implement", lambda *_args, **kwargs:
                        captured.append(kwargs["context"]) or "impl-555-cli")

    result = CliRunner().invoke(crew, [
        "run", "implement requested work", "--project", "test",
        "--db", str(tmp_path / "tasks.db"), "--no-tester", "--max-iter", "1",
    ])

    assert result.exit_code == 0, result.output
    assert captured[0]["base_branch"] == "master"
    assert captured[0]["crew_run_branch"] is False
    assert captured[0]["sync_landed_bases"]["codex"]["sha"] == commits["master"]
    assert _prepare_worktree_for_task(
        str(worker), "impl-555-cli", "main", "implementer", captured[0],
    ) == commits["master"]
