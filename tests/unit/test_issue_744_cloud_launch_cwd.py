"""Cloud CLI must launch from the task repository's validated checkout (#744)."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


SHA = "a" * 40
SESSION = "session_abc12345"


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def _checkout(tmp_path, origin="https://github.com/owner/repo.git"):
    remote = tmp_path / "remote.git"
    _git("init", "--bare", str(remote))
    checkout = tmp_path / "worktrees" / "project" / "claude"
    checkout.mkdir(parents=True)
    _git("init", "-b", "main", cwd=checkout)
    _git("config", "user.email", "test@example.com", cwd=checkout)
    _git("config", "user.name", "Test", cwd=checkout)
    (checkout / "README.md").write_text("fixture\n")
    _git("add", "README.md", cwd=checkout)
    _git("commit", "-m", "fixture", cwd=checkout)
    _git("remote", "add", "origin", origin, cwd=checkout)
    _git("config", f"url.file://{remote}/.insteadOf", origin, cwd=checkout)
    _git("push", "origin", "HEAD:refs/heads/main", cwd=checkout)
    return checkout


def _state(tmp_path, checkout):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"project": "project", "worktrees": {"claude": str(checkout)}}))
    return str(path)


def _task(queue):
    queue.enqueue(TaskRequest(task_id="review-744", task_type="review",
                              description="Review PR", branch="agent/branch",
                              project="project", context={"repo": "owner/repo",
                              "reviewed_sha": SHA, "pr_number": 744, "risk_tier": 2}))


def _run(launches):
    def run(argv, **kwargs):
        if "--help" in argv:
            return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
        launches.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "session_id": SESSION, "url": f"https://claude.ai/code/{SESSION}"}))
    return run


def test_launch_uses_checkout_and_receipt_matches_repo(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path)
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _task(queue)
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    launches = []
    outcome = cloud.dispatch_cloud_for_role(queue, role="reviewer", task_type="review",
                                             state_path=_state(tmp_path, checkout),
                                             run_fn=_run(launches))
    assert outcome.dispatched
    assert len(launches) == 1
    assert launches[0][1]["cwd"] == str(checkout)
    receipt = queue.get_task_context("review-744")["cloud_dispatch_receipt"]
    assert receipt["cwd_remote"] == receipt["repo"] == "owner/repo"


@pytest.mark.parametrize("kind", ["missing", "wrong_repo", "unpublished_branch"])
def test_unusable_checkout_fails_closed_without_launch(tmp_path, monkeypatch, kind):
    checkout = _checkout(tmp_path,
                         "git@github.com:someone/other.git" if kind == "wrong_repo"
                         else "https://github.com/owner/repo.git")
    if kind == "missing":
        checkout.rename(checkout.with_name("removed"))
    elif kind == "unpublished_branch":
        _git("checkout", "-b", "local-only", cwd=checkout)
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _task(queue)
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    launches = []
    outcome = cloud.dispatch_cloud_for_role(queue, role="reviewer", task_type="review",
                                             state_path=_state(tmp_path, checkout),
                                             run_fn=_run(launches))
    assert not outcome.dispatched
    assert "cloud_review_cwd_repo_mismatch" in (outcome.launch_error or "")
    assert launches == []


def test_ssh_origin_matches_https_repo(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path, "git@github.com:owner/repo.git")
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _task(queue)
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    launches = []
    outcome = cloud.dispatch_cloud_for_role(queue, role="reviewer", task_type="review",
                                             state_path=_state(tmp_path, checkout),
                                             run_fn=_run(launches))
    assert outcome.dispatched
    assert len(launches) == 1


def test_shadow_launch_uses_same_checkout_and_fails_closed_when_missing(tmp_path, monkeypatch):
    checkout = _checkout(tmp_path)
    state_path = _state(tmp_path, checkout)
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _task(queue)
    task = queue.dequeue(role="reviewer")
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setenv("AGENT_CREW_CLOUD_SHADOW", "1")
    monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_CONCURRENCY", "3")
    monkeypatch.delenv("AGENT_CREW_CLOUD_MAX_PER_DAY", raising=False)
    launches = []
    assert cloud.launch_shadow_review(queue, task, state_path=state_path,
                                      run_fn=_run(launches))
    assert launches[0][1]["cwd"] == str(checkout)
    receipt = queue.get_task_context(task.task_id)["cloud_shadow_dispatch_receipt"]
    assert receipt["cwd_remote"] == receipt["repo"] == "owner/repo"

    second = TaskRequest(task_id="review-744-missing", task_type="review",
                         description="Review PR", project="project",
                         context={"repo": "owner/repo", "reviewed_sha": SHA,
                                  "pr_number": 745, "risk_tier": 2})
    queue.enqueue(second)
    second = queue.dequeue(role="reviewer")
    checkout.rename(checkout.with_name("removed"))
    assert not cloud.launch_shadow_review(queue, second, state_path=state_path,
                                          run_fn=_run(launches))
    assert len(launches) == 1
    assert queue.get_task_context(second.task_id)["cloud_shadow_terminal_error"] == (
        "cloud_review_cwd_repo_mismatch")
