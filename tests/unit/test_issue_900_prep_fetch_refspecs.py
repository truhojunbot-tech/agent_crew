"""#900: bounded, explicit fetches and fail-closed implement preparation."""

import asyncio
import subprocess

import pytest
from fastapi.testclient import TestClient

from agent_crew import pipeline, server
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


def test_pipeline_fetch_uses_explicit_refs_overrides_and_runtime_timeout(monkeypatch):
    calls = []
    monkeypatch.setenv("AGENT_CREW_GIT_FETCH_TIMEOUT_S", "27")
    monkeypatch.setattr(pipeline.subprocess, "run", lambda argv, **kw: (
        calls.append((argv, kw)) or subprocess.CompletedProcess(argv, 0, "", "")))

    pipeline.git_fetch_origin("/repo", "main", "fix/work", "main")
    argv, kwargs = calls[0]
    assert argv == [
        "git", "-C", "/repo", "-c", "gc.auto=0", "-c",
        "maintenance.auto=false", "fetch", "origin",
        "+refs/heads/main:refs/remotes/origin/main",
        "+refs/heads/fix/work:refs/remotes/origin/fix/work", "--quiet",
    ]
    assert kwargs["timeout"] == 27
    monkeypatch.setenv("AGENT_CREW_GIT_FETCH_TIMEOUT_S", "31")
    pipeline._git("/repo", "fetch", "origin", pipeline.git_fetch_refspec("main"))
    assert calls[-1][0][3:7] == ["-c", "gc.auto=0", "-c", "maintenance.auto=false"]
    assert calls[-1][1]["timeout"] == 31


def test_server_prep_fetches_base_and_continuing_branch_with_shared_timeout(
    tmp_path, monkeypatch,
):
    origin = tmp_path / "origin.git"
    worker = tmp_path / "worker"
    real_run = subprocess.run

    def git(cwd, *args):
        return real_run(["git", *args], cwd=cwd, check=True,
                        capture_output=True, text=True).stdout.strip()

    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", str(origin), str(worker))
    git(worker, "config", "user.email", "crew@example.test")
    git(worker, "config", "user.name", "Crew Test")
    (worker / "base.txt").write_text("base\n")
    git(worker, "add", ".")
    git(worker, "commit", "-m", "base")
    git(worker, "push", "origin", "main")
    git(worker, "switch", "-c", "fix/work")
    git(worker, "push", "origin", "fix/work")

    calls = []
    def recording_run(argv, **kwargs):
        if "fetch" in argv:
            calls.append((argv, kwargs))
        return real_run(argv, **kwargs)

    monkeypatch.setenv("AGENT_CREW_GIT_FETCH_TIMEOUT_S", "23")
    monkeypatch.setattr(pipeline.subprocess, "run", recording_run)
    server._prepare_worktree_for_task(
        str(worker), "impl-900", "fix/work", "implementer",
        {"pr_number": 900}, "implement",
    )
    assert [call[0][9] for call in calls] == [
        "+refs/heads/main:refs/remotes/origin/main",
        "+refs/heads/fix/work:refs/remotes/origin/fix/work",
    ]
    assert all(call[0][3:7] == ["-c", "gc.auto=0", "-c", "maintenance.auto=false"]
               and call[1]["timeout"] == 23 for call in calls)


def test_failed_implement_fetch_refuses_dispatch_without_using_head(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    origin = tmp_path / "origin.git"
    worker = tmp_path / "worker"
    real_run = subprocess.run
    def git(cwd, *args):
        return real_run(["git", *args], cwd=cwd, check=True,
                        capture_output=True, text=True).stdout.strip()
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", str(origin), str(worker))
    git(worker, "config", "user.email", "crew@example.test")
    git(worker, "config", "user.name", "Crew Test")
    (worker / "base.txt").write_text("base\n")
    git(worker, "add", ".")
    git(worker, "commit", "-m", "base")
    git(worker, "push", "origin", "main")
    git(worker, "switch", "-c", "unrelated")
    (worker / "other.txt").write_text("other task\n")
    git(worker, "add", ".")
    git(worker, "commit", "-m", "other task")
    unrelated_head = git(worker, "rev-parse", "HEAD")
    assert unrelated_head != git(worker, "rev-parse", "origin/main")
    db = str(tmp_path / "tasks.db")
    notices = []
    app = server.create_app(
        db, worktree_map={"implementer": str(worker)},
        identity_required=False, watchdog_disabled=True, anomaly_disabled=True,
        coordinator_notify_fn=lambda message: notices.append(message) or True,
    )
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest("impl-900", "implement", "work", branch="fix/work",
                              project="demo", context={"risk_tier": 1}))
    monkeypatch.setattr(server, "_ensure_role_protocol", lambda *a, **kw: True)
    def fetch_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(["git", "fetch", "origin"], 23)
    monkeypatch.setattr(pipeline.subprocess, "run", lambda argv, **kw:
                        fetch_timeout() if "fetch" in argv else real_run(argv, **kw))
    launches = []
    async def no_launch(*args, **kwargs):
        launches.append(args)
        raise AssertionError("worker launched after failed fetch")
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", no_launch)

    with TestClient(app):
        task = queue.dequeue(role="implementer")
        assert task is not None
        asyncio.run(app.state.dispatch_task(task, "implementer"))

    assert launches == []
    assert queue.get_task_status("impl-900") == "needs_human"
    assert queue.get_result("impl-900").error_info["reason"] == "prep_fetch_failed"
    assert queue.get_task_context("impl-900").get("prep_error_base_source") != "head"
    assert git(worker, "rev-parse", "HEAD") == unrelated_head
    assert len(notices) == 1
    assert "prep_fetch_failed" in notices[0]


@pytest.mark.parametrize("continuing", [False, True])
def test_pinned_implement_base_survives_unavailable_origin(tmp_path, continuing):
    origin = tmp_path / "origin.git"
    worker = tmp_path / "worker"
    def git(cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, check=True,
                              capture_output=True, text=True).stdout.strip()
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", str(origin), str(worker))
    git(worker, "config", "user.email", "crew@example.test")
    git(worker, "config", "user.name", "Crew Test")
    (worker / "base.txt").write_text("base\n")
    git(worker, "add", ".")
    git(worker, "commit", "-m", "base")
    pinned = git(worker, "rev-parse", "HEAD")
    git(worker, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    context = {"base_branch": "main", "worktree_base_sha": pinned}
    if continuing:
        context["pr_number"] = 900

    prepared = server._prepare_worktree_for_task(
        str(worker), "impl-900-pinned", "fix/new", "implementer",
        context, "implement",
    )
    assert prepared == pinned
    assert git(worker, "rev-parse", "HEAD") == pinned


def test_fetched_continuing_branch_survives_base_fetch_failure(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    worker = tmp_path / "worker"
    real_run = subprocess.run
    def git(cwd, *args):
        return real_run(["git", *args], cwd=cwd, check=True,
                        capture_output=True, text=True).stdout.strip()
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", str(origin), str(worker))
    git(worker, "config", "user.email", "crew@example.test")
    git(worker, "config", "user.name", "Crew Test")
    (worker / "base.txt").write_text("base\n")
    git(worker, "add", ".")
    git(worker, "commit", "-m", "base")
    git(worker, "push", "origin", "main")
    git(worker, "switch", "-c", "fix/work")
    (worker / "work.txt").write_text("work\n")
    git(worker, "add", ".")
    git(worker, "commit", "-m", "work")
    expected = git(worker, "rev-parse", "HEAD")
    git(worker, "push", "origin", "fix/work")
    actual_fetch = server.git_fetch_origin
    def fetch(repo, branch):
        if branch == "main":
            return subprocess.CompletedProcess([], 128, "", "origin/main unavailable")
        return actual_fetch(repo, branch)
    monkeypatch.setattr(server, "git_fetch_origin", fetch)

    prepared = server._prepare_worktree_for_task(
        str(worker), "impl-900-fix", "fix/work", "implementer",
        {"base_branch": "main", "pr_number": 900}, "implement",
    )
    assert prepared == expected
