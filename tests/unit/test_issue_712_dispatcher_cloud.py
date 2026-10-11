"""#712: headless dispatcher routes cloud reviews through the existing adapter."""

import json
import sqlite3
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


SHA = "a" * 40
SESSION = "session_abc12345"


def _state(tmp_path):
    roles = []
    for role, agent in (("implementer", "codex"), ("reviewer", "claude_cloud"),
                        ("tester", "gemini")):
        worktree = tmp_path / f"wt_{role}"
        worktree.mkdir()
        (worktree / ".git").mkdir()
        roles.append({"role": role, "agent": agent, "worktree": str(worktree)})
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"project": "agent_crew", "role_agents": {
        "implementer": "codex", "reviewer": "claude_cloud", "tester": "gemini"},
        "roles": roles}))
    return str(path)


def _enqueue_review(queue, task_id="review-cloud"):
    pr_number = 713 if task_id == "review-next" else 712
    queue.enqueue(TaskRequest(
        task_id=task_id, task_type="review", description="Review pinned PR",
        branch=f"agent/{task_id}", project="agent_crew",
        context={"pr_number": pr_number, "reviewed_sha": SHA, "repo": "owner/repo",
                 "risk_tier": 2},
    ))


def _wait_for(predicate, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    assert predicate()


def test_headless_dispatcher_launches_enabled_cloud_review(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _enqueue_review(queue)
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", ".02")
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.delenv("AGENT_CREW_CLOUD_MAX_PER_DAY", raising=False)
    launches = []

    def run(argv, **kwargs):
        if "--help" in argv:
            return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
        launches.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "session_id": SESSION, "url": f"https://claude.ai/code/{SESSION}"}))

    monkeypatch.setattr(cloud, "_default_run", run)
    app = create_app(queue._db_path, state_path=_state(tmp_path),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        _wait_for(lambda: queue.get_task("review-cloud").status == "in_progress"
                  and queue.get_attribution("review-cloud") is not None)
    assert len(launches) == 1
    assert queue.get_attribution("review-cloud")["provider_session_id"] == SESSION
    context = queue.get_task_context("review-cloud")
    assert context["cloud_session_id"] == SESSION
    assert context["cloud_launched_at"] > 0


def test_cloud_daily_cap_uses_durable_launch_count(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _enqueue_review(queue, "review-prior")
    queue.record_attribution(task_id="review-prior", agent="claude_cloud",
                             role="reviewer", task_type="review",
                             provider_session_id=SESSION, started_at=time.time())
    _enqueue_review(queue, "review-next")
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_PER_DAY", "1")
    outcome = cloud.dispatch_cloud_for_role(queue, role="reviewer", task_type="review",
        run_fn=lambda argv: (_ for _ in ()).throw(AssertionError("CLI invoked over cap")))
    assert outcome.skipped_reason == "daily_cap"
    assert queue.get_task("review-next").status == "pending"


def test_cloud_review_without_result_goes_stale_not_approved(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    task = _enqueue_review(queue)
    task = queue.dequeue(role="reviewer")
    queue.record_dispatch(task.task_id, channel=cloud.DISPATCH_CHANNEL,
                          agent=cloud.CLOUD_PROVIDER_NAME, target="pending")
    monkeypatch.setenv("AGENT_CREW_CLOUD_STALE_SECONDS", "1")
    monkeypatch.setattr(queue, "get_dispatched_at", lambda _: time.time() - 10)
    monkeypatch.setattr("agent_crew.pipeline.auto_fallback_failed_task",
                        lambda *_args, **_kwargs: None)
    outcome = cloud.reconcile_cloud_dispatch(
        queue, task, pr_comments_fn=lambda *_args, **_kwargs: [],
        submit_review_result_fn=lambda *_args: None)
    assert outcome.action == "failed"
    assert queue.get_task(task.task_id).status == "failed"
    assert queue.get_result(task.task_id).verdict is None
