"""A crew run branch starts at its declared base and survives review rounds."""

import subprocess

from agent_crew.server import _prepare_worktree_for_task


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_missing_run_branch_is_created_from_declared_base_and_keeps_new_commit(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "test")
    (clone / "file.txt").write_text("base\n")
    _git(clone, "add", "file.txt")
    _git(clone, "commit", "-m", "base")
    _git(clone, "push", "origin", "main")
    base_sha = _git(clone, "rev-parse", "HEAD")
    worker = tmp_path / "worker"
    _git(clone, "worktree", "add", "--detach", str(worker), "origin/main")

    branch = "fix/348-target"
    context = {"crew_run_branch": True, "base_branch": "main",
               "worktree_base_sha": base_sha}
    first = _prepare_worktree_for_task(str(worker), "impl-1", branch,
                                       "implementer", context)
    assert first == base_sha
    assert _git(worker, "symbolic-ref", "--short", "HEAD") == branch

    (worker / "file.txt").write_text("changed\n")
    _git(worker, "commit", "-am", "change")
    _git(worker, "push", "origin", f"HEAD:{branch}")
    new_sha = _git(worker, "rev-parse", "HEAD")
    second = _prepare_worktree_for_task(str(worker), "impl-2", branch,
                                        "implementer", context)
    assert second == new_sha
    assert _git(worker, "symbolic-ref", "--short", "HEAD") == branch
