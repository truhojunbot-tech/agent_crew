"""A crew run branch starts at its declared base without owning caller refs."""

import subprocess

from agent_crew.server import _prepare_worktree_for_task


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_missing_run_branch_starts_detached_from_declared_base_and_keeps_new_commit(tmp_path):
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
    assert subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=worker,
                          capture_output=True).returncode != 0
    assert subprocess.run(["git", "show-ref", "--verify", f"refs/heads/{branch}"],
                          cwd=worker, capture_output=True).returncode != 0

    (worker / "file.txt").write_text("changed\n")
    _git(worker, "commit", "-am", "change")
    _git(worker, "push", "origin", f"HEAD:refs/heads/{branch}")
    new_sha = _git(worker, "rev-parse", "HEAD")
    second = _prepare_worktree_for_task(str(worker), "impl-2", branch,
                                        "implementer", context)
    assert second == new_sha
    assert subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=worker,
                          capture_output=True).returncode != 0


def test_run_flag_never_owns_an_existing_callers_branch(tmp_path):
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
    branch = "feat/devs-work"
    _git(clone, "checkout", "-b", branch)
    _git(clone, "push", "origin", branch)
    (clone / "file.txt").write_text("local-only\n")
    _git(clone, "commit", "-am", "developer work")
    expected = _git(clone, "rev-parse", "HEAD")
    _git(clone, "checkout", "main")
    worker = tmp_path / "worker"
    _git(clone, "worktree", "add", "--detach", str(worker), "origin/main")

    prepared = _prepare_worktree_for_task(
        str(worker), "impl-existing", branch, "implementer",
        {"crew_run_branch": True, "base_branch": "main"},
    )

    assert prepared == expected
    assert _git(clone, "rev-parse", branch) == expected
    assert subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=worker,
                          capture_output=True).returncode != 0
    assert subprocess.run(["git", "checkout", branch], cwd=clone,
                          capture_output=True).returncode == 0


def test_caller_supplied_run_flag_cannot_take_main_ref(tmp_path):
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
    expected = _git(clone, "rev-parse", "main")
    _git(clone, "checkout", "--detach", "HEAD")
    worker = tmp_path / "worker"
    _git(clone, "worktree", "add", "--detach", str(worker), "origin/main")

    _prepare_worktree_for_task(str(worker), "impl-main", "main", "implementer",
                               {"crew_run_branch": True, "base_branch": "main"})

    assert _git(clone, "rev-parse", "main") == expected
    assert subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=worker,
                          capture_output=True).returncode != 0
