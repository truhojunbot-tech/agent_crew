"""Council #39 enforcement: risk determines automated cascade spend."""

from agent_crew.pipeline import auto_enqueue_fix, auto_enqueue_review, auto_enqueue_test
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.risk_tier import classify_task, effective_fix_round_cap
from agent_crew.telemetry import TaskTelemetry


def _open(_pr):
    return "open"


def _task(queue, task_id):
    return next(task for task in queue.list_tasks() if task.task_id == task_id)


def test_classifier_honours_explicit_override_and_metadata():
    assert classify_task("update docs/README.md", {"risk_tier": 3}) == 3
    assert classify_task("update docs/README.md", {}) == 0
    assert classify_task("change src/agent_crew/queue.py", {}) == 2
    assert classify_task("add internal unit test", {}) == 1
    assert classify_task("run production deploy", {}) == 3
    assert classify_task("small cleanup", {"changed_paths": ["src/agent_crew/pause.py"]}) == 3
    assert classify_task("small cleanup", {"changed_paths": ["docs/runbook.md"]}) == 0


def test_low_tiers_reduce_automatic_cascade_and_fix_budget(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("doc", "implement", "docs/README.md only", branch="b"))
    assert auto_enqueue_review(queue, "doc", pr_number=1, pr_state_fn=_open) is None
    queue.enqueue(TaskRequest("internal", "implement", "internal helper with unit tests",
                              branch="b", context={"issue": 39}))
    review_id = auto_enqueue_review(queue, "internal", pr_number=2, pr_state_fn=_open)
    review = _task(queue, review_id)
    assert review.context["risk_tier"] == 1
    assert effective_fix_round_cap(review.context) == 1
    queue.submit_result(review_id, TaskResult(task_id=review_id, status="completed",
                                               summary="ok", verdict="approve"))
    test_id = auto_enqueue_test(queue, review_id, pr_state_fn=_open)
    assert _task(queue, test_id).context["test_scope"] == "targeted"


def test_tier_two_marks_adversarial_review_and_tier_three_requires_gate(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("core", "implement", "change src/agent_crew/queue.py", branch="b"))
    review_id = auto_enqueue_review(queue, "core", pr_number=3, pr_state_fn=_open)
    assert _task(queue, review_id).context["review_mode"] == "adversarial"
    queue.enqueue(TaskRequest("deploy", "implement", "deploy release", branch="b"))
    assert auto_enqueue_review(queue, "deploy", pr_number=4, pr_state_fn=_open) is None
    gates = queue.list_gates(status="pending")
    assert len(gates) == 1
    assert gates[0].type == "approval"
    assert "Tier 3" in gates[0].message


def test_cost_summary_preserves_unknowns_and_groups_by_issue(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("known", "implement", "internal", context={"issue": 39}))
    queue.enqueue(TaskRequest("unknown", "implement", "internal", context={"issue": 39}))
    queue.record_attribution("known")
    queue.record_attribution("unknown")
    queue.record_task_telemetry("known", TaskTelemetry(
        uncached_input_tokens=10, cache_write_tokens=2, cache_read_tokens=3,
        output_tokens=5,
    ))
    summary = queue.token_cost_summary()
    assert summary["observed_tasks"] == 1
    assert summary["unobserved_tasks"] == 1
    assert summary["total_tokens"] == 20
    assert summary["by_issue"]["39"]["total_tokens"] == 20
