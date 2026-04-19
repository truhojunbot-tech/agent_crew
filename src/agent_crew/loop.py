import uuid

from agent_crew.protocol import TaskRequest, TaskResult

DEFAULT_MAX_ITER: int = 5

_TDD_CONTEXT = {
    "tdd": True,
    "instructions": "Write tests first (RED), then implement (GREEN), then refactor.",
}

_REVIEW_CONTEXT = {
    "checklist_layers": ["test_quality", "code_quality", "business_gap"],
    "reviewer_rejects_happy_path_only": True,
    "instructions": (
        "3-layer review: "
        "1) test_quality — coverage, edge cases, mocks; "
        "2) code_quality — naming, error handling, SOLID; "
        "3) business_gap — requirements met, logging, observability."
    ),
}


def enqueue_implement(queue, task_desc: str, branch: str, context: dict = {}) -> str:
    req = TaskRequest(
        task_id=f"impl-{uuid.uuid4().hex[:8]}",
        task_type="implement",
        description=task_desc,
        branch=branch,
        context={**_TDD_CONTEXT, **context},
    )
    return queue.enqueue(req)


def enqueue_review(queue, task_desc: str, branch: str, prev_task_id: str, context: dict = {}) -> str:
    req = TaskRequest(
        task_id=f"review-{uuid.uuid4().hex[:8]}",
        task_type="review",
        description=task_desc,
        branch=branch,
        context={**_REVIEW_CONTEXT, "prev_task_id": prev_task_id, **context},
    )
    return queue.enqueue(req)


def enqueue_test(queue, task_desc: str, branch: str, context: dict = {}) -> str:
    req = TaskRequest(
        task_id=f"test-{uuid.uuid4().hex[:8]}",
        task_type="test",
        description=task_desc,
        branch=branch,
        context=context,
    )
    return queue.enqueue(req)


def handle_review_result(
    result: TaskResult,
    iteration: int,
    max_iter: int,
    no_tester: bool = False,
) -> str:
    if iteration >= max_iter and result.verdict != "approve":
        return "escalate"
    if result.verdict == "approve":
        return "approved"
    return "request_changes"


def handle_test_result(result: TaskResult) -> str:
    if result.status == "completed":
        return "passed"
    return "failed"


def build_feedback(result: TaskResult) -> str:
    lines = [f"Review feedback (task {result.task_id}):"]
    for finding in result.findings:
        lines.append(f"- {finding}")
    return "\n".join(lines)
