"""An approved review's pin survives into the test it creates (#824).

Observed on alfred :8103 (build e695aaa, 2026-10-22 05:56): the test of an
approved fix-round review (r1-r1) was created carrying the review's
``reviewed_sha``, then the tester's worktree prep hit a ``git fetch`` timeout.
Prep returned "" and the dispatch recorded ``reviewed_sha: null`` over the pin,
so gemini refused a task block that no longer named its revision.
"""

import json
import pytest
pytestmark = pytest.mark.usefixtures("legacy_gemini_tester_policy")
import subprocess

from fastapi.testclient import TestClient

from agent_crew import server as sv
from agent_crew.pipeline import auto_enqueue_test
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue

from tests.unit.test_issue_812_prep_error_base import _app, _dispatch_pending, _git, _repo, _timeout


def _tester_app(tmp_path, monkeypatch, worker, pushed):
    """The 812 harness with an agent tester: the script tester (#772) never
    prepares a worktree, so it would not reach the path this issue is about."""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "role_agents": {"implementer": "codex", "reviewer": "claude", "tester": "gemini"},
    }))
    real_create_app = sv.create_app
    monkeypatch.setattr(sv, "create_app", lambda db, **kw: real_create_app(
        db, state_path=str(state), project="demo", **kw))
    app, queue = _app(tmp_path, monkeypatch, worker, pushed, role="tester")
    return app, queue


def _two_commit_repo(tmp_path):
    """A worker whose HEAD is NOT the pinned commit, so recovery must move it."""
    worker, base = _repo(tmp_path)
    _git(worker, "checkout", "-q", "-b", "fix/824")
    (worker / "fix.txt").write_text("fix\n")
    _git(worker, "add", "fix.txt")
    _git(worker, "commit", "-m", "fix")
    pin = _git(worker, "rev-parse", "HEAD")
    _git(worker, "checkout", "-q", "main")
    assert _git(worker, "rev-parse", "HEAD") == base != pin
    return worker, pin


def test_test_from_two_round_fix_chain_carries_the_approving_review_pin(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    shas = {n: f"{n}" * 40 for n in "abc"}
    chain = [
        ("impl-824", "implement", {}),
        ("review-impl-824-r0", "review", {"reviewed_sha": shas["a"]}),
        ("fix-review-impl-824-r0-r1", "implement", {"fix_round": 1}),
        ("review-fix-review-impl-824-r0-r1-r1", "review",
         {"fix_round": 1, "reviewed_sha": shas["b"]}),
        ("fix-review-fix-review-impl-824-r0-r1-r1-r2", "implement", {"fix_round": 2}),
        ("review-fix-review-fix-review-impl-824-r0-r1-r1-r2-r2", "review",
         {"fix_round": 2, "reviewed_sha": shas["c"]}),
    ]
    prev = None
    for task_id, task_type, extra in chain:
        context = {"pr_number": 824, "repo": "owner/project", **extra}
        if prev:
            context["prev_task_id"] = prev
        queue.enqueue(TaskRequest(task_id=task_id, task_type=task_type,
                                  description=task_id, branch="fix/824",
                                  context=context))
        prev = task_id
    approving = chain[-1][0]
    queue.submit_result(approving, TaskResult(
        task_id=approving, status="completed", summary="approved",
        verdict="approve", findings=[], pr_number=824))

    test_id = auto_enqueue_test(queue, approving, pr_state_fn=lambda _: "open")

    assert test_id == f"test-{approving}"
    context = queue.get_task_context(test_id)
    assert context["reviewed_sha"] == shas["c"]
    assert context["fix_round"] == 2


def _prep_error_name():
    """The class name `_timeout` raises: #896 changed it from TimeoutExpired
    (now retried, never reaching the fallback) to a non-transient error."""
    try:
        _timeout()
    except Exception as exc:  # noqa: BLE001 - only its type name is wanted
        return type(exc).__name__
    raise AssertionError("_timeout must raise")


def test_tester_prep_error_keeps_and_checks_out_the_review_pin(tmp_path, monkeypatch):
    worker, pin = _two_commit_repo(tmp_path)
    pushed = []
    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _timeout)
    app, queue = _tester_app(tmp_path, monkeypatch, worker, pushed)

    with TestClient(app) as client:
        admitted = client.post("/tasks", json={
            "task_id": "test-review-824", "task_type": "test",
            "description": "test PR", "branch": "fix/824", "project": "demo",
            "context": {"pr_number": 824, "reviewed_sha": pin, "fix_round": 2},
        })
        launched = _dispatch_pending(app, queue, "tester", "test-review-824", monkeypatch)

    assert admitted.status_code == 201
    context = queue.get_task_context("test-review-824")
    assert context["reviewed_sha"] == pin
    assert context["worktree_base_status"] == "caller_after_prep_error"
    assert context["prep_error"] == _prep_error_name()
    assert _git(worker, "rev-parse", "HEAD") == pin
    assert queue.get_task_status("test-review-824") != "needs_human"
    assert launched


def test_reviewer_prefers_requeue_expected_head_over_stale_pin(tmp_path, monkeypatch):
    worker, pin = _two_commit_repo(tmp_path)
    base = _git(worker, "rev-parse", "HEAD")
    context = {"reviewed_sha": base, "expected_head_sha": pin}
    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _timeout)

    assert sv._prepare_worktree_for_task(str(worker), "review-824", "fix/824",
                                         "reviewer", task_context=context) == pin
    assert context["prep_error_base_source"] == "caller"
    assert _git(worker, "rev-parse", "HEAD") == pin


def test_unresolvable_tester_pin_still_refuses_after_prep_error(tmp_path, monkeypatch):
    worker, head = _repo(tmp_path)
    pushed = []
    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _timeout)
    app, queue = _tester_app(tmp_path, monkeypatch, worker, pushed)

    with TestClient(app) as client:
        admitted = client.post("/tasks", json={
            "task_id": "test-review-824-missing", "task_type": "test",
            "description": "test PR", "branch": "fix/824", "project": "demo",
            "context": {"pr_number": 824, "reviewed_sha": "d" * 40},
        })
        launched = _dispatch_pending(app, queue, "tester", "test-review-824-missing", monkeypatch)

    assert admitted.status_code == 201
    assert not launched
    assert queue.get_task_status("test-review-824-missing") == "needs_human"
    assert queue.get_task_context("test-review-824-missing").get("reviewed_sha") != head
    assert _git(worker, "rev-parse", "HEAD") == head


def test_symbolic_pin_is_never_checked_out_after_prep_error(tmp_path, monkeypatch):
    worker, head = _repo(tmp_path)
    context = {"reviewed_sha": "origin/main"}
    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", _timeout)
    calls = []
    real_run = subprocess.run
    monkeypatch.setattr(sv.subprocess, "run",
                        lambda cmd, *a, **kw: calls.append(cmd) or real_run(cmd, *a, **kw))

    assert sv._prepare_worktree_for_task(str(worker), "test-824-sym", "main",
                                         "tester", task_context=context) == ""
    assert not any("checkout" in cmd for cmd in calls)
    assert "prep_error_base_source" not in context
