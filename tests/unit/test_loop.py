from unittest.mock import MagicMock, patch
import io
import urllib.error

import pytest

from agent_crew.loop import (
    DEFAULT_MAX_ITER,
    build_feedback,
    enqueue_implement,
    enqueue_review,
    enqueue_test,
    handle_review_result,
    handle_test_result,
)
from agent_crew.protocol import TaskResult


# U-L01: enqueue_implement — task_type=implement, TDD 지시 context 포함
def test_u_l01_enqueue_implement():
    queue = MagicMock()
    queue.enqueue.side_effect = lambda req, **kwargs: req.task_id

    task_id = enqueue_implement(queue, "Add login feature", "feat/login")

    assert isinstance(task_id, str)
    assert queue.enqueue.call_count == 1
    assert queue.enqueue.call_args.kwargs["ingress"] == "loop.implement"
    req = queue.enqueue.call_args[0][0]
    assert req.task_type == "implement"
    assert "tdd" in str(req.context).lower() or "test" in str(req.context).lower()


# U-L02: enqueue_review — task_type=review, 3-layer 체크리스트 context 포함
def test_u_l02_enqueue_review():
    queue = MagicMock()
    queue.enqueue.side_effect = lambda req, **kwargs: req.task_id

    task_id = enqueue_review(queue, "Add login feature", "feat/login", prev_task_id="impl-001")

    assert isinstance(task_id, str)
    req = queue.enqueue.call_args[0][0]
    assert req.task_type == "review"
    assert queue.enqueue.call_args.kwargs["ingress"] == "loop.review"
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


# U-L04: handle_review_result + no_tester=True → "approved" (허용 어휘: approved/request_changes/escalate)
def test_u_l04_handle_review_result_no_tester():
    result = TaskResult(
        task_id="r-001",
        status="completed",
        summary="Looks good",
        verdict="approve",
    )
    assert handle_review_result(result, iteration=1, max_iter=DEFAULT_MAX_ITER, no_tester=True) == "approved"
    assert handle_review_result(result, iteration=1, max_iter=DEFAULT_MAX_ITER, no_tester=False) == "approved"


# U-L05: handle_review_result verdict=request_changes → "request_changes"
def test_u_l05_handle_review_result_request_changes():
    # #376: a rejection is actionable only when it names at least one finding.
    result = TaskResult(
        task_id="r-002",
        status="completed",
        summary="Needs work",
        verdict="request_changes",
        findings=["src/loop.py:1: correct the reported behavior"],
    )
    assert handle_review_result(result, iteration=1, max_iter=DEFAULT_MAX_ITER) == "request_changes"


# U-L06: handle_review_result iteration >= max_iter → "escalate"
def test_u_l06_handle_review_result_escalate():
    # #376: exercise the iteration cap with a valid rejection, not silence.
    result = TaskResult(
        task_id="r-003",
        status="completed",
        summary="Still needs work",
        verdict="request_changes",
        findings=["src/loop.py:1: correct the reported behavior"],
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
    queue.enqueue.side_effect = lambda req, **kwargs: req.task_id

    enqueue_review(queue, "Add login feature", "feat/login", prev_task_id="impl-001")

    req = queue.enqueue.call_args[0][0]
    ctx_str = str(req.context).lower()
    assert "happy_path" in ctx_str or "happy path" in ctx_str or "reviewer_rejects" in ctx_str


# U-L10: build_feedback — findings에 layer 레이블 prefix 포함, unknown 정규화
def test_u_l10_build_feedback():
    result = TaskResult(
        task_id="r-001",
        status="completed",
        summary="Issues found",
        findings=[
            "test_quality: missing edge cases",
            "code_quality: no error handling",
            "business_gap: no logging",
            "no label here",
        ],
    )
    feedback = build_feedback(result)

    assert "[test_quality]" in feedback
    assert "[code_quality]" in feedback
    assert "[business_gap]" in feedback
    assert "missing edge cases" in feedback
    assert "[unknown]" in feedback


# U-L10b: handle_test_result status=needs_human → "needs_human"
def test_u_l10b_handle_test_result_needs_human():
    result = TaskResult(
        task_id="t-003",
        status="needs_human",
        summary="Requires human review",
    )
    assert handle_test_result(result) == "needs_human"


# U-L10c: build_feedback — dict finding의 알 수 없는 layer → [unknown] 정규화
def test_u_l10c_build_feedback_dict_unknown_layer():
    result = TaskResult(
        task_id="r-002",
        status="completed",
        summary="Issues found",
        findings=[
            {"layer": "typo", "issue": "misnamed variable"},
            {"layer": "test_quality", "issue": "missing branch coverage"},
        ],
    )
    feedback = build_feedback(result)

    assert "[unknown]" in feedback
    assert "misnamed variable" in feedback
    assert "[test_quality]" in feedback
    assert "missing branch coverage" in feedback


# U-L11: port > 0 → POSTs to /tasks HTTP endpoint (triggers server push).
# Without this path, direct queue.enqueue() bypasses the server's push_fn and
# tasks sit QUEUED without ever reaching the tmux pane.
def test_u_l11_enqueue_implement_routes_through_http_when_port_given():
    queue = MagicMock()
    queue.project_identity = "test-project"
    fake_resp = MagicMock()
    fake_resp.read.return_value = b'{"task_id":"impl-http-1"}'
    fake_resp.__enter__ = lambda s: s
    fake_resp.__exit__ = MagicMock(return_value=False)

    with patch("agent_crew.loop.urllib.request.urlopen", return_value=fake_resp) as mock_urlopen:
        task_id = enqueue_implement(queue, "t", "main", port=8101)

    assert task_id == "impl-http-1"
    queue.enqueue.assert_not_called()
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "http://127.0.0.1:8101/tasks"
    assert req.get_method() == "POST"


# U-L12: port == 0 → direct queue.enqueue (legacy path, no server).
def test_u_l12_enqueue_implement_direct_db_when_no_port():
    queue = MagicMock()
    queue.enqueue.side_effect = lambda req, **kwargs: req.task_id

    with patch("agent_crew.loop.urllib.request.urlopen") as mock_urlopen:
        task_id = enqueue_implement(queue, "t", "main")

    assert task_id.startswith("impl-")
    queue.enqueue.assert_called_once()
    assert queue.enqueue.call_args.kwargs["ingress"] == "loop.implement"
    mock_urlopen.assert_not_called()


# U-L13: enqueue_review / enqueue_test also honor port.
def test_u_l13_enqueue_review_and_test_honor_port():
    queue = MagicMock()
    queue.project_identity = "test-project"
    fake_resp = MagicMock()
    fake_resp.read.return_value = b'{"task_id":"x"}'
    fake_resp.__enter__ = lambda s: s
    fake_resp.__exit__ = MagicMock(return_value=False)

    with patch("agent_crew.loop.urllib.request.urlopen", return_value=fake_resp) as mock_urlopen:
        enqueue_review(queue, "t", "main", prev_task_id="impl-1", port=8101)
        enqueue_test(queue, "t", "main", port=8101)

    queue.enqueue.assert_not_called()
    assert mock_urlopen.call_count == 2


# U-L14: enqueue_review is idempotent — if review already exists for impl task, return its ID
def test_u_l14_enqueue_review_idempotent():
    from agent_crew.protocol import TaskRequest

    queue = MagicMock()
    existing_review = TaskRequest(
        task_id="review-existing",
        task_type="review",
        description="some work",
        branch="main",
        context={"prev_task_id": "impl-1"},
    )
    queue.list_tasks.return_value = [existing_review]

    task_id = enqueue_review(queue, "some work", "main", prev_task_id="impl-1")

    assert task_id == "review-existing"
    queue.enqueue.assert_not_called()  # No new task created


# U-L15: enqueue_review creates new if no existing review for impl task
def test_u_l15_enqueue_review_creates_new_if_none_exist():
    queue = MagicMock()
    queue.list_tasks.return_value = []  # No existing tasks
    queue.enqueue.side_effect = lambda req, **kwargs: req.task_id

    task_id = enqueue_review(queue, "some work", "main", prev_task_id="impl-1")

    assert task_id.startswith("review-")
    queue.enqueue.assert_called_once()
    req = queue.enqueue.call_args[0][0]
    assert req.task_type == "review"
    assert req.context.get("prev_task_id") == "impl-1"


def test_http_duplicate_adopts_server_successor_by_id():
    """The HTTP 409 names the task that won the race after the first lookup."""
    from agent_crew.protocol import TaskRequest

    existing = TaskRequest(
        task_id="review-server", task_type="review", description="work",
        branch="main", context={"prev_task_id": "impl-1", "reviewed_sha": "a" * 40},
    )
    queue = MagicMock()
    queue.list_tasks.return_value = []
    queue.get_task.return_value = existing
    body = io.BytesIO(b'{"detail":{"error":"DUPLICATE_REVIEW",'
                      b'"existing_task_id":"review-server"}}')
    conflict = urllib.error.HTTPError("http://localhost/tasks", 409, "Conflict", {}, body)
    with patch("agent_crew.loop._post_task_http", side_effect=conflict):
        task_id = enqueue_review(
            queue, "work", "main", prev_task_id="impl-1",
            context={"reviewed_sha": "a" * 40}, port=8105)

    assert task_id == "review-server"
    queue.enqueue.assert_not_called()


@pytest.mark.parametrize("stage", ["review", "test"])
@pytest.mark.parametrize("transport", ["in_process", "http"])
@pytest.mark.parametrize("collision", [
    "wrong_sha", "wrong_type", "unrelated_parent", "same_sha_other_parent",
])
def test_duplicate_successor_adoption_checks_stage_and_reviewed_head(
        stage, transport, collision):
    """A 409 may name another task; only the same lineage or head is safe."""
    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import DuplicateReviewError

    parent = "impl-current" if stage == "review" else "review-current"
    old_parent = "other-parent" if collision in {
        "unrelated_parent", "same_sha_other_parent"} else parent
    old_stage = ("test" if stage == "review" else "review") if collision == "wrong_type" else stage
    old_sha = "b" * 40 if collision == "wrong_sha" else "a" * 40
    old_context = {"prev_task_id": old_parent}
    if collision != "unrelated_parent":
        old_context["reviewed_sha"] = old_sha
    existing = TaskRequest(
        task_id="server-successor", task_type=old_stage, description="work",
        branch="main", status="completed", context=old_context,
    )
    queue = MagicMock()
    queue.list_tasks.return_value = []  # Server wins after CLI's first lookup.
    queue.get_task.return_value = existing
    enqueue = enqueue_review if stage == "review" else enqueue_test
    kwargs = {"prev_task_id": parent, "context": {"reviewed_sha": "a" * 40}}

    if transport == "in_process":
        conflict = DuplicateReviewError(existing.task_id)
        queue.enqueue.side_effect = conflict
        call = lambda: enqueue(queue, "work", "main", **kwargs)
        error_type = DuplicateReviewError
        conflict_patch = patch("agent_crew.loop._post_task_http")
    else:
        body = io.BytesIO(b'{"detail":{"error":"DUPLICATE_REVIEW",'
                          b'"existing_task_id":"server-successor"}}')
        conflict = urllib.error.HTTPError("http://localhost/tasks", 409, "Conflict", {}, body)
        call = lambda: enqueue(queue, "work", "main", port=8105, **kwargs)
        error_type = urllib.error.HTTPError
        conflict_patch = patch("agent_crew.loop._post_task_http", side_effect=conflict)

    # Avoid the five-second visibility poll when the named row is unsafe.
    with conflict_patch, patch("agent_crew.loop._successor_after_conflict", return_value=None):
        if collision == "same_sha_other_parent":
            assert call() == existing.task_id
        else:
            with pytest.raises(error_type) as raised:
                call()
            assert raised.value is conflict
    if transport == "http":
        queue.enqueue.assert_not_called()
