"""A transient prep timeout is retried once before needs_human (#896)."""

import asyncio
import subprocess
import time

from fastapi.testclient import TestClient

from agent_crew import server as sv
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


def _app(tmp_path, monkeypatch, pushed):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE__DEMO", "off")
    monkeypatch.setattr(sv, "_pane_has_usage_limit", lambda *a, **kw: False)
    monkeypatch.setattr(sv, "_resolve_tmux_pane_target", lambda pane: pane)
    monkeypatch.setattr(sv, "_pane_alive_for_push", lambda pane: True)
    monkeypatch.setattr(sv, "_pane_process_kind", lambda pane: ("agent", "codex"))
    monkeypatch.setattr(sv, "_ensure_role_protocol", lambda *a, **kw: True)
    monkeypatch.setattr(sv, "_restore_task_wip", lambda *a, **kw: None)
    worker = tmp_path / "worker"
    worker.mkdir()
    db = str(tmp_path / "tasks.db")
    app = sv.create_app(
        db, worktree_map={"reviewer": str(worker)}, pane_map={"reviewer": "%91"},
        push_fn=lambda *args: pushed.append(args),
        identity_required=False, watchdog_disabled=True, anomaly_disabled=True,
    )
    return app, TaskQueue(db)


def _timeout(*args, **kwargs):
    raise subprocess.TimeoutExpired(["git", "fetch", "origin"], 60)


def _no_spawn(monkeypatch):
    async def fake_exec(*args, **kwargs):
        raise RuntimeError("provider launch stubbed out")
    monkeypatch.setattr(sv.asyncio, "create_subprocess_exec", fake_exec)


def _enqueue(queue, task_id):
    queue.enqueue(TaskRequest(task_id, "review", "review PR", branch="main",
                              project="demo", context={"pr_number": 999}))
    task = queue.dequeue(role="reviewer")
    assert task is not None and task.task_id == task_id
    return task


def test_first_prep_timeout_requeues_with_backoff(tmp_path, monkeypatch):
    pushed = []
    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _timeout)
    _no_spawn(monkeypatch)
    app, queue = _app(tmp_path, monkeypatch, pushed)

    with TestClient(app):
        task = _enqueue(queue, "review-896-once")
        before = time.time()
        asyncio.run(app.state.dispatch_task(task, "reviewer"))

    assert queue.get_task_status("review-896-once") == "pending"
    assert queue.get_result("review-896-once") is None
    ctx = queue.get_task_context("review-896-once")
    assert ctx["prep_retry_count"] == 1
    assert ctx["push_refusal_reason"] == "prep_transient"
    assert ctx["push_not_before"] > before
    # The backoff is honoured by the dispatcher's dequeue, not only the push path.
    assert queue.dequeue(role="reviewer") is None


def test_prep_timeout_then_success_dispatches(tmp_path, monkeypatch):
    pushed = []
    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _timeout)
    _no_spawn(monkeypatch)
    app, queue = _app(tmp_path, monkeypatch, pushed)

    with TestClient(app):
        task = _enqueue(queue, "review-896-ok")
        asyncio.run(app.state.dispatch_task(task, "reviewer"))
        assert queue.get_task_status("review-896-ok") == "pending"

        queue.merge_task_context("review-896-ok", {"push_not_before": 0})
        sha = "a" * 40
        monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner",
                            lambda *a, **kw: sha)
        task = queue.dequeue(role="reviewer")
        assert task is not None and task.task_id == "review-896-ok"
        asyncio.run(app.state.dispatch_task(task, "reviewer"))

    ctx = queue.get_task_context("review-896-ok")
    assert ctx["reviewed_sha"] == sha
    assert ctx.get("prep_error") is None
    result = queue.get_result("review-896-ok")
    assert result is None or result.error_info.get("reason") != "prepared_base_not_recorded"


def test_two_prep_timeouts_go_to_needs_human(tmp_path, monkeypatch):
    pushed = []
    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _timeout)
    _no_spawn(monkeypatch)
    app, queue = _app(tmp_path, monkeypatch, pushed)

    with TestClient(app):
        task = _enqueue(queue, "review-896-twice")
        asyncio.run(app.state.dispatch_task(task, "reviewer"))
        queue.merge_task_context("review-896-twice", {"push_not_before": 0})
        task = queue.dequeue(role="reviewer")
        assert task is not None
        asyncio.run(app.state.dispatch_task(task, "reviewer"))

    assert queue.get_task_status("review-896-twice") == "needs_human"
    info = queue.get_result("review-896-twice").error_info
    assert info["reason"] == "prepared_base_not_recorded"
    assert info["prep_error"] == "TimeoutExpired"
    assert info["prep_retry_count"] == 1


def test_non_transient_prep_error_is_unchanged(tmp_path, monkeypatch):
    pushed = []

    def _broken(*args, **kwargs):
        raise subprocess.CalledProcessError(128, ["git", "checkout"])

    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _broken)
    _no_spawn(monkeypatch)
    app, queue = _app(tmp_path, monkeypatch, pushed)

    with TestClient(app):
        task = _enqueue(queue, "review-896-broken")
        asyncio.run(app.state.dispatch_task(task, "reviewer"))

    assert queue.get_task_status("review-896-broken") == "needs_human"
    ctx = queue.get_task_context("review-896-broken")
    assert ctx.get("prep_retry_count") is None
    assert queue.get_result("review-896-broken").error_info["reason"] == "prepared_base_not_recorded"


def test_push_path_retries_transient_prep_once(tmp_path, monkeypatch):
    pushed = []
    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _timeout)
    app, queue = _app(tmp_path, monkeypatch, pushed)

    with TestClient(app) as client:
        admitted = client.post("/tasks", json={
            "task_id": "review-896-push", "task_type": "review",
            "description": "review PR", "branch": "main", "project": "demo",
            "context": {"pr_number": 999},
        })

    assert admitted.status_code == 201
    assert queue.get_task_status("review-896-push") == "pending"
    assert queue.get_task_context("review-896-push")["prep_retry_count"] == 1
    assert pushed == []


# ── §8 re-admission: the retry moves the receipt, never strands the row ────────

from agent_crew.cea.engine import EngineConfig  # noqa: E402
from tests.unit.sev0_cea_acceptance_helpers import (  # noqa: E402
    AuthorityState, queue_for, receipt_for_task, task)


def _claimed(tmp_path, name, *, config=None):
    q = queue_for(tmp_path, AuthorityState("active"), name=name, mode="test", config=config)
    q.enqueue(task("t1", context={"authority_decision_ids": ["T0-1234"]}),
              ingress="http.tasks")
    assert q.dequeue(agent="claude", role="implementer") is not None
    return q


def test_prep_retry_reuses_the_receipt_while_an_attempt_remains(tmp_path):
    q = _claimed(tmp_path, "reuse.db",
                 config=EngineConfig(mode="test", default_max_attempts=3))
    before = receipt_for_task(q, "t1")
    assert q.defer_prep_retry("t1", delay_s=0, prep_error="TimeoutExpired") is True
    assert q.get_task_status("t1") == "pending"
    after = receipt_for_task(q, "t1")
    assert after["receipt_id"] == before["receipt_id"]
    assert (after["state"], after["attempt"]) == ("HELD", 2)
    assert q.get_task_context("t1")["prep_retry_count"] == 1


def test_prep_retry_on_a_spent_receipt_leaves_the_task_for_needs_human(tmp_path):
    q = _claimed(tmp_path, "spent.db")      # default max_attempts=1: RE_ADMIT
    assert q.defer_prep_retry("t1", delay_s=0, prep_error="TimeoutExpired") is False
    assert q.get_task_status("t1") == "in_progress"
    assert receipt_for_task(q, "t1")["state"] == "CLAIMED"
    assert q.get_task_context("t1").get("prep_retry_count") is None


# ── implementer: a fallback SHA must not skip the retry (review-impl-896p-r0) ──

def _impl_repo(tmp_path):
    worker = tmp_path / "impl-worker"
    subprocess.run(["git", "init", "--initial-branch=main", str(worker)],
                   check=True, capture_output=True)
    for args in (["config", "user.name", "T"], ["config", "user.email", "t@example.invalid"],
                 ["commit", "--allow-empty", "-m", "base"]):
        subprocess.run(["git", "-C", str(worker), *args], check=True, capture_output=True)
    head = subprocess.run(["git", "-C", str(worker), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()
    return worker, head


def _impl_app(tmp_path, monkeypatch, pushed, worker):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE__DEMO", "off")
    monkeypatch.setattr(sv, "_pane_has_usage_limit", lambda *a, **kw: False)
    monkeypatch.setattr(sv, "_resolve_tmux_pane_target", lambda pane: pane)
    monkeypatch.setattr(sv, "_pane_alive_for_push", lambda pane: True)
    monkeypatch.setattr(sv, "_pane_process_kind", lambda pane: ("agent", "codex"))
    monkeypatch.setattr(sv, "_ensure_role_protocol", lambda *a, **kw: True)
    monkeypatch.setattr(sv, "_restore_task_wip", lambda *a, **kw: None)
    db = str(tmp_path / "tasks.db")
    app = sv.create_app(
        db, worktree_map={"implementer": str(worker)}, pane_map={"implementer": "%92"},
        push_fn=lambda *args: pushed.append(args),
        identity_required=False, watchdog_disabled=True, anomaly_disabled=True,
    )
    return app, TaskQueue(db)


def test_implementer_head_fallback_still_retries_on_dispatcher_path(tmp_path, monkeypatch):
    worker, head = _impl_repo(tmp_path)
    pushed = []
    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _timeout)
    launched = []

    async def fake_exec(*args, **kwargs):
        launched.append(args)
        raise RuntimeError("provider launch stubbed out")
    monkeypatch.setattr(sv.asyncio, "create_subprocess_exec", fake_exec)
    app, queue = _impl_app(tmp_path, monkeypatch, pushed, worker)

    with TestClient(app):
        queue.enqueue(TaskRequest("impl-896-disp", "implement", "fix", branch="feature",
                                  project="demo", context={}))
        task = queue.dequeue(role="implementer")
        assert task is not None
        # Precondition the review found: prep returns the HEAD fallback, not "".
        assert sv._prepare_worktree_for_task(str(worker), task.task_id, "feature",
                                             "implementer", task_context=dict(task.context)) == head
        asyncio.run(app.state.dispatch_task(task, "implementer"))

    assert launched == []
    assert queue.get_task_status("impl-896-disp") == "pending"
    ctx = queue.get_task_context("impl-896-disp")
    assert ctx["prep_retry_count"] == 1
    assert ctx["push_refusal_reason"] == "prep_transient"
    # The stale HEAD was not recorded as the base, so a retry cannot re-read it
    # as a "caller" base.
    assert ctx.get("worktree_base_sha") is None


def test_implementer_head_fallback_still_retries_on_push_path(tmp_path, monkeypatch):
    worker, head = _impl_repo(tmp_path)
    pushed = []
    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _timeout)
    app, queue = _impl_app(tmp_path, monkeypatch, pushed, worker)

    with TestClient(app) as client:
        admitted = client.post("/tasks", json={
            "task_id": "impl-896-push", "task_type": "implement", "description": "fix",
            "branch": "feature", "project": "demo", "context": {},
        })

    assert admitted.status_code == 201
    assert pushed == []
    assert queue.get_task_status("impl-896-push") == "pending"
    ctx = queue.get_task_context("impl-896-push")
    assert ctx["prep_retry_count"] == 1
    assert ctx.get("worktree_base_sha") is None


def _assert_exhausted_needs_human(queue, task_id):
    assert queue.get_task_status(task_id) == "needs_human"
    info = queue.get_result(task_id).error_info
    assert info["reason"] == "prepared_base_not_recorded"
    assert info["prep_error"] == "TimeoutExpired"
    assert info["prep_retry_count"] == 1
    # The stale HEAD fallback was never recorded as this task's base.
    assert queue.get_task_context(task_id).get("worktree_base_sha") is None


def test_implementer_second_timeout_needs_human_on_push_path(tmp_path, monkeypatch):
    """review-fix-review-impl-896p-r0-r1-r1: a nonempty HEAD fallback must not
    turn an exhausted transient prep failure into a push on a stale base."""
    worker, head = _impl_repo(tmp_path)
    pushed = []
    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _timeout)
    app, queue = _impl_app(tmp_path, monkeypatch, pushed, worker)

    with TestClient(app) as client:
        client.post("/tasks", json={
            "task_id": "impl-896-twice", "task_type": "implement", "description": "fix",
            "branch": "feature", "project": "demo", "context": {},
        })
        assert pushed == []
        queue.merge_task_context("impl-896-twice", {"push_not_before": 0})
        app.state.try_push_next("implementer")

    assert pushed == []
    _assert_exhausted_needs_human(queue, "impl-896-twice")


def test_implementer_second_timeout_needs_human_on_dispatcher_path(tmp_path, monkeypatch):
    worker, head = _impl_repo(tmp_path)
    pushed = []
    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _timeout)
    launched = []

    async def fake_exec(*args, **kwargs):
        launched.append(args)
        raise RuntimeError("provider launch stubbed out")
    monkeypatch.setattr(sv.asyncio, "create_subprocess_exec", fake_exec)
    app, queue = _impl_app(tmp_path, monkeypatch, pushed, worker)

    with TestClient(app):
        queue.enqueue(TaskRequest("impl-896-disp2", "implement", "fix", branch="feature",
                                  project="demo", context={}))
        task = queue.dequeue(role="implementer")
        asyncio.run(app.state.dispatch_task(task, "implementer"))
        queue.merge_task_context("impl-896-disp2", {"push_not_before": 0})
        task = queue.dequeue(role="implementer")
        assert task is not None
        assert sv._prepare_worktree_for_task(str(worker), task.task_id, "feature",
                                             "implementer", task_context=dict(task.context)) == head
        asyncio.run(app.state.dispatch_task(task, "implementer"))

    assert launched == [] and pushed == []
    _assert_exhausted_needs_human(queue, "impl-896-disp2")
