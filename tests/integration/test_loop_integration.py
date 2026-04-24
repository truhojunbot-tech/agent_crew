"""
Code-review loop integration tests.

Uses task_queue (direct TaskQueue) for loop.py enqueue functions,
and test_client (HTTP) for mock agent result submission — both share
the same tmp_db, so they see the same SQLite state.
"""

import pytest

from agent_crew.loop import (
    DEFAULT_MAX_ITER,
    build_feedback,
    enqueue_implement,
    enqueue_implement_with_feedback,
    enqueue_review,
    enqueue_test,
    handle_review_result,
    handle_test_result,
)
from agent_crew.protocol import TaskResult


pytestmark = pytest.mark.integration

DESC = "Add login feature"
BRANCH = "feat/login"


def _submit(client, task_id: str, status: str = "completed",
            summary: str = "done", verdict=None, findings=None):
    payload = {"task_id": task_id, "status": status,
               "summary": summary, "findings": findings or []}
    if verdict:
        payload["verdict"] = verdict
    return client.post(f"/tasks/{task_id}/result", json=payload)


def _make_result(task_id: str, status: str = "completed",
                 summary: str = "done", verdict=None, findings=None):
    return TaskResult(
        task_id=task_id,
        status=status,
        summary=summary,
        verdict=verdict,
        findings=findings or [],
    )


# I-LO01: implement → review(approve) → test(pass) → done
def test_i_lo01_approve_first_review(task_queue, test_client):
    impl_id = enqueue_implement(task_queue, DESC, BRANCH)
    _submit(test_client, impl_id, summary="Login implemented")

    review_id = enqueue_review(task_queue, DESC, BRANCH, prev_task_id=impl_id)
    _submit(test_client, review_id, summary="LGTM", verdict="approve")

    outcome = handle_review_result(
        _make_result(review_id, verdict="approve"), iteration=1, max_iter=DEFAULT_MAX_ITER
    )
    assert outcome == "approved"

    test_id = enqueue_test(task_queue, DESC, BRANCH)
    _submit(test_client, test_id, summary="All tests pass")

    assert handle_test_result(_make_result(test_id)) == "passed"

    # server confirms completed tasks
    resp = test_client.get("/tasks", params={"status": "completed"})
    completed_ids = {t["task_id"] for t in resp.json()}
    assert impl_id in completed_ids
    assert review_id in completed_ids
    assert test_id in completed_ids


# I-LO02: request_changes once → re-implement → approve → done
def test_i_lo02_request_changes_then_approve(task_queue, test_client):
    impl_id = enqueue_implement(task_queue, DESC, BRANCH)
    _submit(test_client, impl_id, summary="First attempt")

    review_id = enqueue_review(task_queue, DESC, BRANCH, prev_task_id=impl_id)
    _submit(test_client, review_id, summary="Needs work", verdict="request_changes",
            findings=["code_quality: missing error handling"])

    outcome1 = handle_review_result(
        _make_result(review_id, verdict="request_changes",
                     findings=["code_quality: missing error handling"]),
        iteration=1, max_iter=DEFAULT_MAX_ITER,
    )
    assert outcome1 == "request_changes"

    impl_id2 = enqueue_implement(task_queue, DESC, BRANCH)
    _submit(test_client, impl_id2, summary="Fixed")

    review_id2 = enqueue_review(task_queue, DESC, BRANCH, prev_task_id=impl_id2)
    _submit(test_client, review_id2, summary="LGTM", verdict="approve")

    outcome2 = handle_review_result(
        _make_result(review_id2, verdict="approve"), iteration=2, max_iter=DEFAULT_MAX_ITER
    )
    assert outcome2 == "approved"


# I-LO03: max iterations (5) → escalation gate
def test_i_lo03_max_iterations_escalate(task_queue, test_client):
    prev_id = enqueue_implement(task_queue, DESC, BRANCH)
    _submit(test_client, prev_id, summary="Attempt")

    for i in range(1, DEFAULT_MAX_ITER + 1):
        review_id = enqueue_review(task_queue, DESC, BRANCH, prev_task_id=prev_id)
        _submit(test_client, review_id, summary="Still needs work", verdict="request_changes")

        outcome = handle_review_result(
            _make_result(review_id, verdict="request_changes"),
            iteration=i, max_iter=DEFAULT_MAX_ITER,
            queue=task_queue,
        )
        if i < DEFAULT_MAX_ITER:
            assert outcome == "request_changes"
            prev_id = enqueue_implement(task_queue, DESC, BRANCH)
            _submit(test_client, prev_id, summary=f"Attempt {i + 1}")
        else:
            assert outcome == "escalate"
            # gate auto-created by handle_review_result — verify via HTTP
            resp = test_client.get("/gates/pending")
            pending = resp.json()
            assert any("escalation" in g.get("type", "") for g in pending)


# I-LO04: --no-tester → implement → review(approve) → done, no test enqueued
def test_i_lo04_no_tester(task_queue, test_client):
    impl_id = enqueue_implement(task_queue, DESC, BRANCH)
    # Use queue directly to bypass server auto-enqueue
    task_queue.submit_result(impl_id, _make_result(impl_id, status="completed"))

    review_id = enqueue_review(task_queue, DESC, BRANCH, prev_task_id=impl_id)
    # Use queue directly to avoid triggering server auto-enqueue test
    task_queue.submit_result(review_id, _make_result(review_id, status="completed", verdict="approve"))

    # Verify no test was auto-enqueued (because we bypassed server)
    resp = test_client.get("/tasks", params={"status": "pending"})
    pending = resp.json()
    assert not any(t["task_type"] == "test" for t in pending)


# I-LO05: test failure → re-enqueue implement
def test_i_lo05_test_failure_reimplements(task_queue, test_client):
    impl_id = enqueue_implement(task_queue, DESC, BRANCH)
    _submit(test_client, impl_id, summary="Done")

    review_id = enqueue_review(task_queue, DESC, BRANCH, prev_task_id=impl_id)
    _submit(test_client, review_id, summary="LGTM", verdict="approve")

    handle_review_result(_make_result(review_id, verdict="approve"),
                         iteration=1, max_iter=DEFAULT_MAX_ITER)

    test_id = enqueue_test(task_queue, DESC, BRANCH)
    _submit(test_client, test_id, status="failed", summary="3 tests failed")

    test_outcome = handle_test_result(_make_result(test_id, status="failed"))
    assert test_outcome == "failed"

    # 실패 시 implement 재등록
    impl_id2 = enqueue_implement(task_queue, DESC, BRANCH, context={"retry": True})
    resp = test_client.get(f"/tasks/{impl_id2}")
    assert resp.status_code == 200
    assert resp.json()["task_type"] == "implement"


# I-LO06: reviewer rejects happy-path-only tests → "test_quality" finding
def test_i_lo06_reviewer_rejects_happy_path(task_queue, test_client):
    impl_id = enqueue_implement(task_queue, DESC, BRANCH)
    _submit(test_client, impl_id, summary="Done")

    review_id = enqueue_review(task_queue, DESC, BRANCH, prev_task_id=impl_id)
    _submit(test_client, review_id, summary="Missing edge cases",
            verdict="request_changes",
            findings=["test_quality: only happy path covered, add edge cases"])

    result = _make_result(review_id, verdict="request_changes",
                          findings=["test_quality: only happy path covered, add edge cases"])
    feedback = build_feedback(result)
    assert "[test_quality]" in feedback
    assert "happy path" in feedback or "edge case" in feedback

    outcome = handle_review_result(result, iteration=1, max_iter=DEFAULT_MAX_ITER)
    assert outcome == "request_changes"


# I-LO07: edge cases added → reviewer approves
def test_i_lo07_edge_cases_added_approve(task_queue, test_client):
    # first review rejects
    impl_id = enqueue_implement(task_queue, DESC, BRANCH)
    _submit(test_client, impl_id, summary="First attempt")

    review_id = enqueue_review(task_queue, DESC, BRANCH, prev_task_id=impl_id)
    feedback_findings = ["test_quality: missing edge cases"]
    _submit(test_client, review_id, verdict="request_changes", findings=feedback_findings)

    result1 = _make_result(review_id, verdict="request_changes", findings=feedback_findings)
    feedback = build_feedback(result1)
    assert "[test_quality]" in feedback

    # second implement with feedback context
    impl_id2 = enqueue_implement(task_queue, DESC, BRANCH, context={"feedback": feedback})
    _submit(test_client, impl_id2, summary="Added edge cases")

    # second review approves
    review_id2 = enqueue_review(task_queue, DESC, BRANCH, prev_task_id=impl_id2)
    _submit(test_client, review_id2, summary="Edge cases covered, LGTM", verdict="approve")

    outcome = handle_review_result(
        _make_result(review_id2, verdict="approve"), iteration=2, max_iter=DEFAULT_MAX_ITER
    )
    assert outcome == "approved"


# I-LO08: business_gap finding carried into next implement context
def test_i_lo08_business_gap_in_context(task_queue, test_client):
    impl_id = enqueue_implement(task_queue, DESC, BRANCH)
    _submit(test_client, impl_id, summary="Done")

    review_id = enqueue_review(task_queue, DESC, BRANCH, prev_task_id=impl_id)
    gap_findings = ["business_gap: no audit logging for auth events"]
    _submit(test_client, review_id, verdict="request_changes", findings=gap_findings)

    result = _make_result(review_id, verdict="request_changes", findings=gap_findings)

    # build_feedback가 [business_gap] prefix를 생성하는지 확인
    feedback = build_feedback(result)
    assert "[business_gap]" in feedback

    # enqueue_implement_with_feedback가 feedback을 context에 자동 carryover
    impl_id2 = enqueue_implement_with_feedback(task_queue, DESC, BRANCH, result)

    resp = test_client.get(f"/tasks/{impl_id2}")
    assert resp.status_code == 200
    ctx = resp.json()["context"]
    assert "feedback" in ctx
    assert "[business_gap]" in ctx["feedback"]
