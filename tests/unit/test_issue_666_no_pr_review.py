"""A completed implement without a reviewable PR must not spend a reviewer."""

from unittest.mock import patch

from agent_crew.pipeline import auto_enqueue_review
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


def _completed_impl(tmp_path, *, crew_run_branch=False, branch="agent/work"):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(
        task_id="impl-no-pr", task_type="implement", description="Implement #666",
        branch=branch, context={"repo": "owner/repo", "crew_run_branch": crew_run_branch},
    ))
    queue.submit_result("impl-no-pr", TaskResult(
        task_id="impl-no-pr", status="completed", summary="pushed",
        branch=branch, commit="a" * 40,
    ))
    return queue


def _reviews(queue):
    return [task for task in queue.list_tasks() if task.task_type == "review"]


def test_no_pr_without_cli_branch_sends_one_notice_and_no_review(tmp_path):
    queue = _completed_impl(tmp_path)
    messages = []
    with patch("agent_crew.github.pr_number_for_branch", return_value=None), patch(
        "agent_crew.notify.notify_telegram", side_effect=lambda message: messages.append(message) or True
    ):
        assert auto_enqueue_review(queue, "impl-no-pr") is None
        assert auto_enqueue_review(queue, "impl-no-pr") is None
    assert _reviews(queue) == []
    assert queue.get_task("impl-no-pr").context["review_blocked_reason"] == "no_pr_for_review"
    assert len(messages) == 1
    assert "no_pr_for_review" in messages[0]


def test_existing_pr_number_is_carried_to_review(tmp_path):
    queue = _completed_impl(tmp_path)
    with patch("agent_crew.github.pr_number_for_branch", return_value=666) as lookup:
        review_id = auto_enqueue_review(queue, "impl-no-pr", pr_state_fn=lambda number: "open")
    lookup.assert_called_once_with("agent/work", repo="owner/repo", state="open")
    assert review_id is not None
    assert len(_reviews(queue)) == 1
    assert _reviews(queue)[0].context["pr_number"] == 666


def test_cli_branch_can_still_enqueue_before_pr_exists(tmp_path):
    queue = _completed_impl(tmp_path, crew_run_branch=True)
    with patch("agent_crew.github.pr_number_for_branch", return_value=None):
        review_id = auto_enqueue_review(queue, "impl-no-pr")
    assert review_id is not None
    assert len(_reviews(queue)) == 1


def test_no_branch_and_no_pr_keeps_161_guard(tmp_path):
    queue = _completed_impl(tmp_path, branch="")
    with patch("agent_crew.github.pr_number_for_branch", return_value=None):
        assert auto_enqueue_review(queue, "impl-no-pr") is None
    assert _reviews(queue) == []
