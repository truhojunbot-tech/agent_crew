"""A broken cascade is visible, and an unknown review status is not adopted."""

from types import SimpleNamespace

import pytest

from agent_crew import loop, pipeline
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


@pytest.mark.parametrize("step", ["review", "fix", "test"])
def test_enqueue_exception_records_typed_reason_and_one_notice(tmp_db, monkeypatch, step):
    queue = TaskQueue(tmp_db)
    if step == "review":
        task = TaskRequest(task_id="impl-932", task_type="implement",
                           description="implement", branch="agent/932",
                           context={"crew_run_branch": True})
        result = TaskResult(task_id=task.task_id, status="completed", summary="done")
        enqueue = lambda notify: pipeline.auto_enqueue_review(
            queue, task.task_id, coordinator_notify_fn=notify)
    else:
        task = TaskRequest(task_id="review-932", task_type="review",
                           description="review", branch="agent/932", context={})
        result = TaskResult(task_id=task.task_id, status="completed",
                            summary="reviewed", verdict=(
                                "request_changes" if step == "fix" else "approve"),
                            findings=(["HIGH x.py:1 - fix it"] if step == "fix" else []))
        enqueue = (lambda notify: pipeline.auto_enqueue_fix(
            queue, task.task_id, pr_state_fn=lambda _: "open",
            coordinator_notify_fn=notify)) if step == "fix" else (
            lambda notify: pipeline.auto_enqueue_test(
                queue, task.task_id, pr_state_fn=lambda _: "open",
                coordinator_notify_fn=notify))
    queue.enqueue(task)
    queue.submit_result(task.task_id, result)
    monkeypatch.setattr(pipeline, "_enqueue_with_inherited_risk",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("broken")))
    sent = []
    notify = lambda message: sent.append(message) or True
    for _ in range(2):
        assert enqueue(notify) is None
    info = queue.get_task(task.task_id).error_info
    assert info["reason"] == "cascade_enqueue_failed"
    assert info["kind"] == "config_error"
    assert info["cascade_step"] == step
    assert len(sent) == 1
    assert "reason_code=cascade_enqueue_failed" in sent[0]


def test_unknown_review_status_is_not_adopted(monkeypatch):
    task = SimpleNamespace(task_id="review-932", task_type="review",
                           context={"prev_task_id": "impl-932"})
    queue = SimpleNamespace(
        list_tasks=lambda: [task],
        get_task_status=lambda _: (_ for _ in ()).throw(RuntimeError("lookup failed")))
    monkeypatch.setattr(loop, "_patch_adopted_review_context",
                        lambda *args: pytest.fail("must not adopt unknown review"))
    assert loop._existing_successor_id(queue, "review", "impl-932") is None
