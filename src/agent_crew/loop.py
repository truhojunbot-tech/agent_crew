import uuid

from agent_crew.protocol import GateRequest, TaskRequest, TaskResult

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


_KNOWN_LAYERS = {"test_quality", "code_quality", "business_gap"}


def handle_review_result(
    result: TaskResult,
    iteration: int,
    max_iter: int,
    no_tester: bool = False,
    queue=None,
    task_desc: str = "",
    branch: str = "",
) -> str:
    if iteration >= max_iter and result.verdict != "approve":
        if queue is not None:
            gate = GateRequest(
                id=f"gate-escalation-{uuid.uuid4().hex[:8]}",
                type="escalation",
                message=f"Review loop exceeded {max_iter} iterations",
            )
            queue.create_gate(gate)
        return "escalate"
    if result.verdict == "approve":
        if queue is not None and not no_tester and task_desc and branch:
            enqueue_test(queue, task_desc, branch)
        return "approved"
    return "request_changes"


def enqueue_implement_with_feedback(
    queue, task_desc: str, branch: str, review_result: TaskResult
) -> str:
    feedback = build_feedback(review_result)
    return enqueue_implement(queue, task_desc, branch, context={"feedback": feedback})


def handle_test_result(result: TaskResult) -> str:
    if result.status == "completed":
        return "passed"
    if result.status == "needs_human":
        return "needs_human"
    return "failed"


def build_feedback(result: TaskResult) -> str:
    lines = [f"Review feedback (task {result.task_id}):"]
    for finding in result.findings:
        if isinstance(finding, dict):
            layer = finding.get("layer", "unknown")
            if layer not in _KNOWN_LAYERS:
                layer = "unknown"
            issue = finding.get("issue", str(finding))
        else:
            parts = str(finding).split(":", 1)
            layer = parts[0].strip() if len(parts) == 2 and parts[0].strip() in _KNOWN_LAYERS else "unknown"
            issue = parts[1].strip() if layer != "unknown" else str(finding)
        lines.append(f"- [{layer}] {issue}")
    return "\n".join(lines)
