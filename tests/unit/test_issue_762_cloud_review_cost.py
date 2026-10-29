"""Cloud review credit intervals, including comparison reviews (#762)."""
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


SHA = "a" * 40
SESSION = "session_abc12345"


def _comment(task_id):
    return "<!-- agent_crew:cloud-review " + json.dumps({
        "task_id": task_id, "reviewed_sha": SHA,
        "verdict": "approve", "findings": [],
    }) + " -->"


def _review(queue, task_id="review-cost"):
    queue.enqueue(TaskRequest(task_id=task_id, task_type="review", description="Review PR",
                              branch=f"agent/{task_id}", project="agent_crew",
                              context={"pr_number": {"review-cost": 762, "first": 763,
                                                     "second": 764}.get(task_id, 765),
                                       "reviewed_sha": SHA,
                                       "repo": "owner/repo", "risk_tier": 2}))


def _runner(argv, **kwargs):
    if "--help" in argv:
        return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
    return SimpleNamespace(returncode=0, stdout=json.dumps(
        {"session_id": SESSION, "url": f"https://claude.ai/code/{SESSION}"}))


@pytest.fixture
def cloud_env(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setenv("AGENT_CREW_CLOUD_SHADOW", "1")
    monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_CONCURRENCY", "3")
    monkeypatch.delenv("AGENT_CREW_CLOUD_MAX_PER_DAY", raising=False)
    monkeypatch.setattr(cloud, "_validated_cloud_checkout",
                        lambda *_args: ("/fixture/claude", "owner/repo"))


def test_marked_review_records_credit_at_launch_and_completion(tmp_path, monkeypatch, cloud_env):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _review(queue)
    credit = iter([10.0, 10.4])
    monkeypatch.setattr(cloud, "read_cloud_credit_used", lambda: next(credit))
    outcome = cloud.dispatch_cloud_for_role(queue, role="reviewer", task_type="review",
                                            run_fn=_runner)
    assert outcome.dispatched
    assert queue.get_attribution("review-cost")["cloud_credit_used_before"] == 10.0
    comment = _comment("review-cost")
    monkeypatch.setattr(cloud._github, "pr_comments", lambda *_a, **_kw: [{"body": comment}])
    outcomes = cloud.reconcile_all_cloud_tasks(
        queue, submit_review_result_fn=lambda task_id, result: queue.submit_result(task_id, result))
    assert outcomes[0].action == "review_completed"
    assert queue.get_attribution("review-cost")["cloud_usd"] is None
    assert queue.get_attribution("review-cost")["cloud_cost_status"] == "PENDING_SETTLE"


@pytest.mark.parametrize("terminal", ["stale", "cancel"])
def test_review_failure_and_cancel_record_credit(tmp_path, monkeypatch, cloud_env, terminal):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _review(queue)
    credit = iter([20.0, 20.2])
    monkeypatch.setattr(cloud, "read_cloud_credit_used", lambda: next(credit))
    cloud.dispatch_cloud_for_role(queue, role="reviewer", task_type="review", run_fn=_runner)
    if terminal == "stale":
        monkeypatch.setenv("AGENT_CREW_CLOUD_STALE_SECONDS", "1")
        monkeypatch.setattr(queue, "get_dispatched_at", lambda _: time.time() - 10)
        monkeypatch.setattr(cloud._github, "pr_comments", lambda *_a, **_kw: [])
        monkeypatch.setattr("agent_crew.pipeline.auto_fallback_failed_task",
                            lambda *_a, **_kw: None)
        cloud.reconcile_all_cloud_tasks(queue)
    else:
        queue.cancel("review-cost")
        cloud.record_terminal_cloud_cost(queue, "review-cost")
    assert queue.get_attribution("review-cost")["cloud_usd"] is None
    assert queue.get_attribution("review-cost")["cloud_cost_status"] == (
        "UNKNOWN_NO_FRESH_READING" if terminal == "stale" else "PENDING_SETTLE")


def test_review_launch_failure_records_credit(tmp_path, monkeypatch, cloud_env):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _review(queue)
    credit = iter([30.0, 30.1])
    monkeypatch.setattr(cloud, "read_cloud_credit_used", lambda: next(credit))
    monkeypatch.setattr("agent_crew.pipeline.auto_fallback_failed_task",
                        lambda *_a, **_kw: None)
    def fail(argv, **kwargs):
        if "--help" in argv:
            return _runner(argv, **kwargs)
        return SimpleNamespace(returncode=3, stdout="failed")
    cloud.dispatch_cloud_for_role(queue, role="reviewer", task_type="review", run_fn=fail)
    assert queue.get_attribution("review-cost")["cloud_usd"] is None
    assert queue.get_attribution("review-cost")["cloud_cost_status"] == "UNKNOWN_NO_FRESH_READING"
    assert queue.get_attribution("review-cost")["status"] == "failed"


@pytest.mark.parametrize("terminal", ["marked", "stale", "launch_failure"])
def test_shadow_review_records_cost_on_every_terminal(tmp_path, monkeypatch, cloud_env, terminal):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _review(queue)
    task = queue.dequeue(role="reviewer")
    credit = iter([40.0, 40.3])
    monkeypatch.setattr(cloud, "read_cloud_credit_used", lambda: next(credit))
    def runner(argv, **kwargs):
        if terminal == "launch_failure" and "--help" not in argv:
            return SimpleNamespace(returncode=3, stdout="failed")
        return _runner(argv, **kwargs)
    launched = cloud.launch_shadow_review(queue, task, run_fn=runner)
    assert launched is (terminal != "launch_failure")
    if terminal == "marked":
        comment = _comment("review-cost")
        cloud.reconcile_shadow_reviews(queue,
            pr_comments_fn=lambda *_a, **_kw: [{"body": comment}])
    elif terminal == "stale":
        monkeypatch.setenv("AGENT_CREW_CLOUD_STALE_SECONDS", "1")
        queue.patch_context("review-cost", {"cloud_shadow_launched_at": time.time() - 10})
        cloud.reconcile_shadow_reviews(queue, pr_comments_fn=lambda *_a, **_kw: [])
    assert queue.get_attribution("cloud-shadow:review-cost")["cloud_usd"] is None
    assert queue.get_attribution("cloud-shadow:review-cost")["cloud_cost_status"] == "PENDING_SETTLE"


def test_overlapping_review_sessions_keep_cost_unknown(tmp_path, monkeypatch, cloud_env):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _review(queue, "first")
    _review(queue, "second")
    credit = iter([50.0, 50.1, 50.4])
    monkeypatch.setattr(cloud, "read_cloud_credit_used", lambda: next(credit))
    cloud.dispatch_cloud_for_role(queue, role="reviewer", task_type="review", run_fn=_runner)
    cloud.dispatch_cloud_for_role(queue, role="reviewer", task_type="review", run_fn=_runner)
    active = [task for task in queue.list_tasks() if task.status == "in_progress"]
    assert len(active) == 2
    queue.cancel(active[0].task_id)
    cloud.record_terminal_cloud_cost(queue, active[0].task_id)
    assert queue.get_attribution(active[0].task_id)["cloud_usd"] is None
    assert queue.get_attribution(active[0].task_id)["cloud_cost_status"] == "PENDING_SETTLE"


def test_shadow_overlap_is_recorded_without_double_counting_daily_launch(tmp_path, monkeypatch,
                                                                          cloud_env):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _review(queue, "first")
    _review(queue, "second")
    shadow_task = queue.dequeue(role="reviewer")
    credit = iter([60.0, 60.1, 60.4])
    monkeypatch.setattr(cloud, "read_cloud_credit_used", lambda: next(credit))
    assert cloud.launch_shadow_review(queue, shadow_task, run_fn=_runner)
    assert cloud.dispatch_cloud_for_role(queue, role="reviewer", task_type="review",
                                         run_fn=_runner).dispatched
    assert queue.count_cloud_reviews_started_since(time.time() - 60) == 2
    comment = _comment("first")
    cloud.reconcile_shadow_reviews(queue,
        pr_comments_fn=lambda *_a, **_kw: [{"body": comment}])
    assert queue.get_attribution("cloud-shadow:first")["cloud_usd"] is None
    assert queue.get_attribution("cloud-shadow:first")["cloud_cost_status"] == "PENDING_SETTLE"


@pytest.mark.parametrize("missing_status_stamp", [False, True])
def test_terminal_legacy_cloud_row_does_not_overlap_new_cost(tmp_path, monkeypatch,
                                                             missing_status_stamp):
    """Pre-#766 terminal rows have a launch time but no cloud_ended_at."""
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(task_id="impl-old", task_type="implement",
                              description="old", branch="agent/old", project="agent_crew",
                              context={"cloud_launched_at": time.time() - 120}))
    queue.cancel("impl-old")
    if missing_status_stamp:
        with sqlite3.connect(queue._db_path) as db:
            db.execute("UPDATE tasks SET status_changed_at=0 WHERE task_id='impl-old'")
    assert "cloud_ended_at" not in queue.get_task_context("impl-old")

    started = time.time()
    monkeypatch.setenv("AGENT_CREW_CLOUD_COST_SETTLE_SECONDS", "0")
    queue.enqueue(TaskRequest(task_id="impl-new", task_type="implement",
                              description="new", branch="agent/new", project="agent_crew",
                              context={"cloud_attempt_started_at": started,
                                       "cloud_credit_used_before": 10.0,
                                       "cloud_credit_before_fetched_at": started - 1,
                                       "cloud_ended_at": started}))
    queue.record_attribution(task_id="impl-new", project="agent_crew",
                             agent="claude_cloud", role="implementer",
                             task_type="implement", provider_session_id="new-session",
                             started_at=started)
    monkeypatch.setattr(cloud, "read_cloud_credit_used",
                        lambda: cloud.CloudCreditSnapshot(10.2, time.time()))
    cloud._record_reconciled_cloud_cost(queue, queue.get_task("impl-new"))
    cloud._record_reconciled_cloud_cost(queue, queue.get_task("impl-new"))
    assert queue.get_attribution("impl-new")["cloud_usd"] == pytest.approx(0.2)
    assert queue.get_task_context("impl-new")["cloud_cost_overlaps"] == []
