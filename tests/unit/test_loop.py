from unittest.mock import MagicMock

from agent_crew.loop import (
    DEFAULT_MAX_ITER,
    build_feedback,
    enqueue_implement,
    enqueue_review,
    handle_review_result,
    handle_test_result,
)
from agent_crew.protocol import TaskResult


# U-L01: enqueue_implement — task_type=implement, TDD 지시 context 포함
def test_u_l01_enqueue_implement():
    queue = MagicMock()
    queue.enqueue.side_effect = lambda req: req.task_id

    task_id = enqueue_implement(queue, "Add login feature", "feat/login")

    assert isinstance(task_id, str)
    assert queue.enqueue.call_count == 1
    req = queue.enqueue.call_args[0][0]
    assert req.task_type == "implement"
    assert "tdd" in str(req.context).lower() or "test" in str(req.context).lower()


# U-L02: enqueue_review — task_type=review, 3-layer 체크리스트 context 포함
def test_u_l02_enqueue_review():
    queue = MagicMock()
    queue.enqueue.side_effect = lambda req: req.task_id

    task_id = enqueue_review(queue, "Add login feature", "feat/login", prev_task_id="impl-001")

    assert isinstance(task_id, str)
    req = queue.enqueue.call_args[0][0]
    assert req.task_type == "review"
    ctx_str = str(req.context).lower()
    assert "layer" in ctx_str or "checklist" in ctx_str or "review" in ctx_str


# U-L03: handle_review_result verdict=approve → "approved"
def test_u_l03_handle_review_result_approve():
    result = TaskResult(
        task_id="r-001",
        status="completed",
        summary="Looks good",
        verdict="approve",
    )
    assert handle_review_result(result, iteration=1, max_iter=DEFAULT_MAX_ITER) == "approved"


# U-L04: handle_review_result + no_tester=True → "approved" (tester 스킵)
def test_u_l04_handle_review_result_no_tester():
    result = TaskResult(
        task_id="r-001",
        status="completed",
        summary="Looks good",
        verdict="approve",
    )
    assert handle_review_result(result, iteration=1, max_iter=DEFAULT_MAX_ITER, no_tester=True) == "approved"


# U-L05: handle_review_result verdict=request_changes → "request_changes"
def test_u_l05_handle_review_result_request_changes():
    result = TaskResult(
        task_id="r-002",
        status="completed",
        summary="Needs work",
        verdict="request_changes",
    )
    assert handle_review_result(result, iteration=1, max_iter=DEFAULT_MAX_ITER) == "request_changes"


# U-L06: handle_review_result iteration >= max_iter → "escalate"
def test_u_l06_handle_review_result_escalate():
    result = TaskResult(
        task_id="r-003",
        status="completed",
        summary="Still needs work",
        verdict="request_changes",
    )
    assert handle_review_result(result, iteration=DEFAULT_MAX_ITER, max_iter=DEFAULT_MAX_ITER) == "escalate"


# U-L07: handle_test_result status=completed → "passed"
def test_u_l07_handle_test_result_passed():
    result = TaskResult(
        task_id="t-001",
        status="completed",
        summary="All tests pass",
    )
    assert handle_test_result(result) == "passed"


# U-L08: handle_test_result status=failed → "failed"
def test_u_l08_handle_test_result_failed():
    result = TaskResult(
        task_id="t-002",
        status="failed",
        summary="3 tests failed",
    )
    assert handle_test_result(result) == "failed"


# U-L09: enqueue_review context에 reviewer_rejects_happy_path_only 지시 포함
def test_u_l09_enqueue_review_rejects_happy_path():
    queue = MagicMock()
    queue.enqueue.side_effect = lambda req: req.task_id

    enqueue_review(queue, "Add login feature", "feat/login", prev_task_id="impl-001")

    req = queue.enqueue.call_args[0][0]
    ctx_str = str(req.context).lower()
    assert "happy_path" in ctx_str or "happy path" in ctx_str or "reviewer_rejects" in ctx_str


# U-L10: build_feedback — findings에 layer 레이블 포함됨
def test_u_l10_build_feedback():
    result = TaskResult(
        task_id="r-001",
        status="completed",
        summary="Issues found",
        findings=["test_quality: missing edge cases", "code_quality: no error handling", "business_gap: no logging"],
    )
    feedback = build_feedback(result)

    assert "test_quality" in feedback
    assert "code_quality" in feedback
    assert "business_gap" in feedback
    assert "missing edge cases" in feedback
