"""#1022: a declared report has no PR by contract — no review, no PR retry."""

from unittest.mock import patch

from agent_crew.pipeline import auto_enqueue_review
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


def _completed_impl(tmp_path, *, artifact_kind=None):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    context = {
        "repo": "owner/repo", "risk_tier": 1,
        "implementer_agent": "codex", "reviewer_agent": "claude",
        "crew_run_branch": False,
    }
    if artifact_kind is not None:
        context["artifact_kind"] = artifact_kind
    queue.enqueue(TaskRequest(
        task_id="impl-1022", task_type="implement", description="Report #1022",
        branch="agent/report", context=context,
    ))
    queue.submit_result("impl-1022", TaskResult(
        task_id="impl-1022", status="completed", summary="reported",
        branch="agent/report", commit="a" * 40,
    ))
    return queue


def _run(queue):
    messages = []
    with patch("agent_crew.github.pr_number_for_branch", return_value=None) as lookup, patch(
        "agent_crew.notify.notify_telegram",
        side_effect=lambda message: messages.append(message) or True,
    ):
        assert auto_enqueue_review(queue, "impl-1022") is None
    return messages, lookup


def test_report_task_without_pr_gets_no_review_retry_or_notice(tmp_path):
    queue = _completed_impl(tmp_path, artifact_kind="report")
    messages, lookup = _run(queue)
    tasks = queue.list_tasks()
    assert [t for t in tasks if t.task_type == "review"] == []
    assert [t for t in tasks if t.task_id.startswith("retry-")] == []
    assert "review_blocked_reason" not in queue.get_task("impl-1022").context
    assert not any("no_pr_for_review" in m for m in messages)
    lookup.assert_not_called()


def test_undeclared_task_without_pr_still_gets_pr_opening_retry(tmp_path):
    queue = _completed_impl(tmp_path)
    messages, _ = _run(queue)
    retry = queue.get_task("retry-impl-1022-a1")
    assert retry is not None
    assert retry.context["retry_reason"] == "no_pr_for_review"
    assert queue.get_task("impl-1022").context["review_blocked_reason"] == "no_pr_for_review"
    assert any("no_pr_for_review" in m for m in messages)
