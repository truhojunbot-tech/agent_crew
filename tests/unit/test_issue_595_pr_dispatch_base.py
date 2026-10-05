"""Existing PR branches use their own head as the implement dispatch base."""

import subprocess

import pytest

from agent_crew.pipeline import verify_implement_artifact
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.server import _prepare_worktree_for_task


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def pr_repo(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "test")
    (clone / "base.txt").write_text("base\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "initial")
    _git(clone, "push", "origin", "main")
    _git(clone, "checkout", "-b", "fix/existing-pr")
    (clone / "pr.txt").write_text("PR change\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "PR head")
    _git(clone, "push", "origin", "fix/existing-pr")
    pr_head = _git(clone, "rev-parse", "HEAD")
    _git(clone, "checkout", "main")
    (clone / "main.txt").write_text("main moved\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "move main")
    _git(clone, "push", "origin", "main")
    main_head = _git(clone, "rev-parse", "HEAD")
    worker = tmp_path / "worker"
    _git(clone, "worktree", "add", "--detach", str(worker), "origin/main")
    assert pr_head != main_head
    return worker, pr_head, main_head


@pytest.mark.parametrize("context", [{"pr_number": 595}, {}])
def test_fix_commit_descending_from_existing_pr_head_is_accepted(pr_repo, context):
    worker, pr_head, main_head = pr_repo
    prepared = _prepare_worktree_for_task(
        str(worker), "fix-review-r2", "fix/existing-pr", "implementer",
        context, task_type="implement")
    assert prepared == pr_head
    assert _git(worker, "rev-parse", "HEAD") == pr_head
    assert prepared != main_head

    (worker / "fix.txt").write_text("review fix\n")
    _git(worker, "add", ".")
    _git(worker, "commit", "-m", "fix review")
    commit = _git(worker, "rev-parse", "HEAD")
    _git(worker, "push", "origin", "HEAD:refs/heads/fix/existing-pr")
    task = TaskRequest("fix-review-r2", "implement", "fix", branch="fix/existing-pr",
                       context={"worktree_base_sha": prepared, **context})
    result = TaskResult("fix-review-r2", "completed", "done",
                        branch="fix/existing-pr", commit=commit)
    assert verify_implement_artifact(task, result, repo_cwd=str(worker))[0]


def test_commit_not_descending_from_pr_head_is_refused(pr_repo):
    worker, pr_head, main_head = pr_repo
    prepared = _prepare_worktree_for_task(
        str(worker), "fix-review-r2", "fix/existing-pr", "implementer",
        {"pr_number": 595}, task_type="implement")
    assert prepared == pr_head
    _git(worker, "checkout", "--detach", main_head)
    (worker / "unrelated.txt").write_text("unrelated\n")
    _git(worker, "add", ".")
    _git(worker, "commit", "-m", "unrelated")
    commit = _git(worker, "rev-parse", "HEAD")
    _git(worker, "push", "origin", "HEAD:refs/heads/fix/diverged")
    task = TaskRequest("fix-review-r2", "implement", "fix", branch="fix/existing-pr",
                       context={"worktree_base_sha": prepared, "pr_number": 595})
    result = TaskResult("fix-review-r2", "completed", "done",
                        branch="fix/diverged", commit=commit)
    accepted, detail = verify_implement_artifact(task, result, repo_cwd=str(worker))
    assert not accepted
    assert detail == "reported commit does not descend from the dispatch base"
