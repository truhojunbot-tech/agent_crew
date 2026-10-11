"""#712: headless dispatcher routes cloud reviews through the existing adapter."""

import json
import sqlite3
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest, TaskResult
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
    pr_number = {"review-next": 713, "review-small": 714,
                 "review-large": 715}.get(task_id, 712)
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


def test_cloud_review_records_result_verdict_and_end_time(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _enqueue_review(queue)
    task = queue.dequeue(role="reviewer")
    queue.record_dispatch(task.task_id, channel=cloud.DISPATCH_CHANNEL,
                          agent=cloud.CLOUD_PROVIDER_NAME, target="pending")
    queue.patch_context(task.task_id, {"cloud_session_id": SESSION,
                                       "cloud_launched_at": time.time() - 1})
    comment = ("[agent_crew review] verdict: approve\n"
               f"reviewed_sha {SHA}\n"
               f"<!-- agent_crew:cloud-review task={task.task_id} sha={SHA} -->")

    outcome = cloud.reconcile_cloud_dispatch(
        queue, task, pr_comments_fn=lambda *_args, **_kw: [{"body": comment}],
        submit_review_result_fn=lambda task_id, result: queue.submit_result(task_id, result))

    assert outcome.action == "review_completed"
    assert queue.get_result(task.task_id).verdict == "approve"
    context = queue.get_task_context(task.task_id)
    assert context["cloud_review_verdict"] == "approve"
    assert context["cloud_ended_at"] > context["cloud_launched_at"]


def test_cloud_claim_prefers_largest_review_context(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _enqueue_review(queue, "review-small")
    _enqueue_review(queue, "review-large")
    queue.patch_context("review-small", {"instructions": "x"})
    queue.patch_context("review-large", {"instructions": "x" * 4000})
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")

    def run(argv, **kwargs):
        if "--help" in argv:
            return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "session_id": SESSION, "url": f"https://claude.ai/code/{SESSION}"}))

    outcome = cloud.dispatch_cloud_for_role(queue, role="reviewer", task_type="review", run_fn=run)
    assert outcome.task_id == "review-large"
    assert queue.get_task("review-small").status == "pending"


def test_shadow_review_records_cloud_without_replacing_local_verdict(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _enqueue_review(queue)
    task = queue.dequeue(role="reviewer")
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setenv("AGENT_CREW_CLOUD_SHADOW", "1")

    def run(argv, **kwargs):
        if "--help" in argv:
            return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "session_id": SESSION, "url": f"https://claude.ai/code/{SESSION}"}))

    assert cloud.launch_shadow_review(queue, task, run_fn=run)
    assert queue.get_task_context(task.task_id)["cloud_shadow_session_id"] == SESSION
    comment = ("[agent_crew review] verdict: request_changes\n"
               f"reviewed_sha {SHA}\n- HIGH src/app.py:1 - fix issue\n"
               f"<!-- agent_crew:cloud-review task={task.task_id} sha={SHA} -->")
    cloud.reconcile_shadow_reviews(queue, pr_comments_fn=lambda *_args, **_kw: [{"body": comment}])
    context = queue.get_task_context(task.task_id)
    assert context["cloud_shadow_verdict"] == "request_changes"
    assert context["cloud_shadow_findings"] == ["HIGH src/app.py:1 - fix issue"]
    assert context["cloud_shadow_ended_at"] > context["cloud_shadow_launched_at"]
    assert queue.get_result(task.task_id) is None
    queue.submit_result(task.task_id, TaskResult(task_id=task.task_id, status="completed",
                                                verdict="approve", summary="local review"))
    cloud.reconcile_shadow_reviews(queue, pr_comments_fn=lambda *_args, **_kw: [])
    assert queue.get_result(task.task_id).verdict == "approve"
    assert queue.get_task_context(task.task_id)["cloud_shadow_local_verdict"] == "approve"


def test_shadow_daily_cap_and_stale_session_leave_local_review_alone(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _enqueue_review(queue)
    task = queue.dequeue(role="reviewer")
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setenv("AGENT_CREW_CLOUD_SHADOW", "1")
    monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_PER_DAY", "0")
    assert not cloud.launch_shadow_review(queue, task,
        run_fn=lambda *_args: (_ for _ in ()).throw(AssertionError("cloud launched over cap")))
    monkeypatch.delenv("AGENT_CREW_CLOUD_MAX_PER_DAY")
    queue.patch_context(task.task_id, {"cloud_shadow_session_id": SESSION,
                                       "cloud_shadow_launched_at": time.time() - 10,
                                       "cloud_shadow_status": "in_progress"})
    monkeypatch.setenv("AGENT_CREW_CLOUD_STALE_SECONDS", "1")
    cloud.reconcile_shadow_reviews(queue, pr_comments_fn=lambda *_args, **_kw: [])
    assert queue.get_task_context(task.task_id)["cloud_shadow_status"] == "failed"
    assert queue.get_task(task.task_id).status == "in_progress"


def test_disabled_cloud_dispatch_does_not_claim_review(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _enqueue_review(queue)
    monkeypatch.delenv("AGENT_CREW_CLOUD_ENABLED", raising=False)
    outcome = cloud.dispatch_cloud_for_role(queue, role="reviewer", task_type="review",
        run_fn=lambda *_args: (_ for _ in ()).throw(AssertionError("disabled cloud invoked")))
    assert outcome.skipped_reason == "disabled"
    assert queue.get_task("review-cloud").status == "pending"


def test_headless_tick_reconciles_cloud_without_watchdog_loop(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", ".02")
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    seen = []
    monkeypatch.setattr(cloud, "reconcile_all_cloud_tasks",
                        lambda *_args, **_kwargs: seen.append("tick"))
    app = create_app(queue._db_path, state_path=_state(tmp_path),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        _wait_for(lambda: bool(seen))


def test_headless_daily_cap_checks_local_reviewer_queue(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", ".02")
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    seen = []
    original_dequeue = TaskQueue.dequeue

    def dequeue(self, *args, **kwargs):
        seen.append((kwargs.get("agent"), kwargs.get("role")))
        return original_dequeue(self, *args, **kwargs)

    monkeypatch.setattr(TaskQueue, "dequeue", dequeue)
    monkeypatch.setattr(cloud, "dispatch_cloud_for_role",
                        lambda *_args, **_kwargs: SimpleNamespace(skipped_reason="daily_cap"))
    app = create_app(queue._db_path, state_path=_state(tmp_path),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        _wait_for(lambda: ("claude_cloud", "reviewer") in seen)


def test_headless_shadow_branch_launches_comparison_before_local_run(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _enqueue_review(queue)
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", ".02")
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setenv("AGENT_CREW_CLOUD_SHADOW", "1")
    launched = []
    monkeypatch.setattr(cloud, "launch_shadow_review",
                        lambda _queue, task: launched.append(task.task_id) or True)
    monkeypatch.setattr(cloud, "reconcile_shadow_reviews", lambda *_args: None)
    app = create_app(queue._db_path, state_path=_state(tmp_path),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        _wait_for(lambda: launched == ["review-cloud"])
