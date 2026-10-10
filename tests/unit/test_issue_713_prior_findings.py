"""#713 part B: a fix-round review carries its triggering findings."""

import pytest

from agent_crew.pipeline import MAX_EMBEDDED_FINDINGS, auto_enqueue_review
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue

PR = 713
BRANCH = "fix/713-prior-findings"


def _open(*_args, **_kwargs):
    return "open"


def _task(queue, task_id):
    return next(task for task in queue.list_tasks() if task.task_id == task_id)


@pytest.mark.parametrize("link_key", ["prev_task_id", "review_task_id"])
def test_fix_round_review_embeds_prior_findings_and_omission_note(tmp_db, link_key):
    queue = TaskQueue(tmp_db)
    findings = [f"HIGH src/module_{i}.py:{i + 1} - finding {i}" for i in range(23)]
    queue.enqueue(TaskRequest(
        task_id="review-prior", task_type="review", description="prior review",
        branch=BRANCH, context={"risk_tier": 1, "pr_number": PR},
    ))
    queue.submit_result("review-prior", TaskResult(
        task_id="review-prior", status="completed",
        summary="Prior review found changes required in the implementation.",
        verdict="request_changes", findings=findings, pr_number=PR,
    ))
    queue.enqueue(TaskRequest(
        task_id="fix-review-prior-r2", task_type="implement", description="fix",
        branch=BRANCH,
        context={link_key: "review-prior", "fix_round": 2,
                 "pr_number": PR, "risk_tier": 1},
    ))
    queue.submit_result("fix-review-prior-r2", TaskResult(
        task_id="fix-review-prior-r2", status="completed", summary="fixed",
        pr_number=PR, branch=BRANCH, commit="a" * 40,
    ))

    review_id = auto_enqueue_review(queue, "fix-review-prior-r2", PR, pr_state_fn=_open)

    assert review_id is not None
    context = _task(queue, review_id).context
    assert context["prior_review_task_id"] == "review-prior"
    instructions = context["instructions"]
    assert ("PRIOR REVIEW FINDINGS (round 2, from review-prior) - verify each "
            "is resolved at the current head; state per finding resolved/unresolved:") in instructions
    for finding in findings[:MAX_EMBEDDED_FINDINGS]:
        assert finding in instructions
    assert findings[MAX_EMBEDDED_FINDINGS] not in instructions
    assert "3 further findings omitted here" in instructions
    assert "GET /tasks/review-prior" in instructions


def test_round_zero_review_instructions_are_unchanged(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(
        task_id="impl-root", task_type="implement", description="implement",
        branch=BRANCH, context={"risk_tier": 1, "pr_number": PR},
    ))
    queue.submit_result("impl-root", TaskResult(
        task_id="impl-root", status="completed", summary="done",
        pr_number=PR, branch=BRANCH, commit="a" * 40,
    ))

    review_id = auto_enqueue_review(queue, "impl-root", PR, pr_state_fn=_open)

    assert review_id is not None
    context = _task(queue, review_id).context
    assert "prior_review_task_id" not in context
    assert "PRIOR REVIEW FINDINGS" not in context["instructions"]
    assert context["instructions"].startswith("3-layer review:")
