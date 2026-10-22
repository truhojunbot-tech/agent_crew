"""#816: a cascade enqueue that hits a transient sqlite lock must be retried.

Observed 2026-10-21 00:23:07Z on 8105: a fix task's result was accepted (200),
``auto_enqueue_review`` logged ``unexpected error: database is locked`` once,
and the lineage simply stopped — no review, no retry, no notice. These tests
drive the real HTTP result path with the lock injected at ``TaskQueue.enqueue``:

  (a) a lock on the first attempt still yields exactly one next task;
  (b) a persistent lock yields one coordinator notice and no task;
  (c) a retry after a partial success (row written, then the lock surfaced)
      creates no duplicate successor.
"""

import sqlite3
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agent_crew import cascade_retry
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app

HEAD = "a" * 40
BRANCH = "agent/claude/816-cascade-lock"
PR = 816
LOCKED = "database is locked"


# ── harness ──────────────────────────────────────────────────────────────


def _harness(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DELIVERY", "off")
    # Background retries sleep between attempts; the test only cares about the
    # attempt count, so make the schedule instantaneous (5 attempts, 0 s).
    monkeypatch.setenv("AGENT_CREW_CASCADE_LOCK_RETRY_DELAYS", "0,0,0,0")
    db = str(tmp_path / "tasks.db")
    notices: list[str] = []
    app = create_app(db, project="agent_crew", pane_map={}, identity_required=False,
                     watchdog_disabled=True, anomaly_disabled=True,
                     coordinator_notify_fn=lambda message: notices.append(message) or True)
    queue = TaskQueue(db)
    return app, queue, notices


def _seed(queue, stage):
    """Seed the parent whose result triggers the ``stage`` cascade."""
    shared = {"repo": "owner/repo", "pr_number": PR, "risk_tier": 1,
              "lineage_root_task_id": "impl-816"}
    if stage == "review":
        queue.enqueue(TaskRequest(
            task_id="impl-816", task_type="implement", description="Fix #816",
            branch=BRANCH, project="agent_crew",
            context={**shared, "issue": 816, "implementer_agent": "claude",
                     "reviewer_agent": "codex"}), ingress="http.tasks")
        return "impl-816"
    queue.enqueue(TaskRequest(
        task_id="impl-816", task_type="implement", description="Fix #816",
        branch=BRANCH, project="agent_crew", context={**shared, "issue": 816}),
        ingress="http.tasks")
    queue.enqueue(TaskRequest(
        task_id="review-impl-816-r0", task_type="review", description="Review PR #816",
        branch=BRANCH, project="agent_crew",
        context={**shared, "prev_task_id": "impl-816", "reviewed_sha": HEAD,
                 "fix_round": 0}), ingress="http.tasks")
    return "review-impl-816-r0"


def _result_payload(stage, parent_id):
    if stage == "review":
        return {"task_id": parent_id, "status": "completed", "summary": "pushed",
                "findings": [], "branch": BRANCH, "commit": HEAD, "pr_number": PR}
    if stage == "test":
        return {"task_id": parent_id, "status": "completed",
                "summary": "approve: the lock retry is bounded and the notice is durable",
                "verdict": "approve", "findings": [], "pr_number": PR}
    return {"task_id": parent_id, "status": "completed",
            "summary": "request_changes: the cascade still drops the lock error silently",
            "verdict": "request_changes", "pr_number": PR,
            "findings": ["HIGH src/agent_crew/server.py:1 - lock dropped"]}


def _successors(queue, stage, parent_id):
    task_type = {"review": "review", "test": "test", "fix": "implement"}[stage]
    return [t for t in queue.list_tasks()
            if t.task_type == task_type and t.task_id != "impl-816"
            and isinstance(t.context, dict) and t.context.get("prev_task_id") == parent_id]


class _LockingEnqueue:
    """Replace ``TaskQueue.enqueue`` so the first ``fail_times`` calls raise
    ``database is locked``. With ``after_insert`` the row is written FIRST and
    the lock surfaces afterwards — the partial-success shape of (c)."""

    def __init__(self, monkeypatch, *, fail_times, after_insert=False):
        self.real = TaskQueue.enqueue
        self.fail_times = fail_times
        self.after_insert = after_insert
        self.calls = 0
        # A plain function so it binds as a method; a callable instance would
        # not receive ``self`` from the attribute lookup.
        monkeypatch.setattr(TaskQueue, "enqueue",
                            lambda queue, task, *a, **kw: self(queue, task, *a, **kw))

    def __call__(self, queue, task, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            if self.after_insert:
                self.real(queue, task, *args, **kwargs)
            raise sqlite3.OperationalError(LOCKED)
        return self.real(queue, task, *args, **kwargs)


def _drive(app, client, stage, parent_id):
    with patch("agent_crew.github.pr_state", return_value="open"), \
            patch("agent_crew.github.pr_head_sha", return_value=HEAD), \
            patch("agent_crew.github.pr_number_for_branch", return_value=PR), \
            patch("agent_crew.github.post_review_comment", return_value=True):
        response = client.post(f"/tasks/{parent_id}/result",
                               json=_result_payload(stage, parent_id))
        assert response.status_code == 200, response.text
        # Host IO here is slow (#816 was measured at load 13-20); the join bound is
        # generous because the delays are 0 and only the attempt count matters.
        assert app.state.cascade_retry_wait(timeout=120), "retry thread did not finish"


STAGES = ["review", "test", "fix"]


# ── (a) one lock, then success → exactly one next task ───────────────────


@pytest.mark.parametrize("stage", STAGES)
def test_lock_on_first_attempt_still_yields_exactly_one_next_task(
        tmp_path, monkeypatch, stage):
    app, queue, notices = _harness(tmp_path, monkeypatch)
    parent_id = _seed(queue, stage)
    lock = _LockingEnqueue(monkeypatch, fail_times=1)
    with TestClient(app) as client:
        _drive(app, client, stage, parent_id)
    assert lock.calls == 2, "one locked attempt, one successful retry"
    assert len(_successors(queue, stage, parent_id)) == 1
    assert notices == []


# ── (b) persistent lock → one notice, no task ────────────────────────────


@pytest.mark.parametrize("stage", STAGES)
def test_persistent_lock_yields_one_notice_and_no_task(tmp_path, monkeypatch, stage):
    app, queue, notices = _harness(tmp_path, monkeypatch)
    parent_id = _seed(queue, stage)
    lock = _LockingEnqueue(monkeypatch, fail_times=10 ** 6)
    with TestClient(app) as client:
        _drive(app, client, stage, parent_id)
    assert lock.calls == 5, "bounded: the inline attempt plus four retries"
    assert _successors(queue, stage, parent_id) == []
    assert len(notices) == 1
    for part in (cascade_retry.NOTICE_KIND, parent_id, "lineage=impl-816",
                 f"missing_next_step={stage}", LOCKED):
        assert part in notices[0], notices[0]
    # The result itself was kept — the cascade stopped, not the audit trail.
    assert queue.get_task_status(parent_id) == "completed"
    # A second exhaustion for the same parent reuses the durable notice claim.
    app.state.coordinator_notice(parent_id, cascade_retry.NOTICE_KIND, "again")
    assert len(notices) == 1


# ── (c) partial success then lock → retry creates no duplicate ───────────


@pytest.mark.parametrize("stage", STAGES)
def test_retry_after_partial_success_creates_no_duplicate(tmp_path, monkeypatch, stage):
    app, queue, notices = _harness(tmp_path, monkeypatch)
    parent_id = _seed(queue, stage)
    lock = _LockingEnqueue(monkeypatch, fail_times=1, after_insert=True)
    with TestClient(app) as client:
        _drive(app, client, stage, parent_id)
    assert lock.calls >= 1
    assert len(_successors(queue, stage, parent_id)) == 1
    assert notices == []


# ── the retry schedule itself ────────────────────────────────────────────


def test_default_schedule_is_five_attempts_over_about_a_minute(monkeypatch):
    monkeypatch.delenv("AGENT_CREW_CASCADE_LOCK_RETRY_DELAYS", raising=False)
    delays = cascade_retry.retry_delays()
    assert len(delays) == 4, "inline attempt + 4 retries = 5 attempts"
    assert 45 <= sum(delays) <= 75


@pytest.mark.parametrize("raw", ["", "abc", "1,-2", "0,0,x"])
def test_malformed_schedule_falls_back_to_default(monkeypatch, raw):
    monkeypatch.setenv("AGENT_CREW_CASCADE_LOCK_RETRY_DELAYS", raw)
    assert cascade_retry.retry_delays() == cascade_retry.DEFAULT_DELAYS


def test_non_lock_errors_are_not_retried():
    """Only ``database is locked`` is transient; everything else stays a
    one-shot warning exactly as before #816."""
    assert cascade_retry.is_database_locked(sqlite3.OperationalError("database is locked"))
    assert not cascade_retry.is_database_locked(sqlite3.OperationalError("no such table: x"))
    assert not cascade_retry.is_database_locked(sqlite3.IntegrityError("database is locked"))
    assert not cascade_retry.is_database_locked(RuntimeError("database is locked"))
