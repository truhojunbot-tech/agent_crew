"""#829: one cloud stale retry, then the existing terminal outcomes."""

import time

import pytest

from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setenv(cloud._ENV_STALE_SECONDS, "1")
    return TaskQueue(str(tmp_path / "tasks.db"))


def _dispatched(queue, task_id, role, context):
    task_type = "review" if role == "reviewer" else "implement"
    queue.enqueue(TaskRequest(
        task_id=task_id, task_type=task_type, description="Cloud task",
        branch="agent/829-work", project="agent_crew",
        context={"repo": "owner/repo", **context},
    ))
    task = queue.dequeue(role=role)
    queue.record_dispatch(task.task_id, channel=cloud.DISPATCH_CHANNEL,
                          agent=cloud.CLOUD_PROVIDER_NAME, target="pending")
    return queue.get_task(task_id)


def _stale(queue, task, monkeypatch):
    monkeypatch.setattr(queue, "get_dispatched_at", lambda _: time.time() - 10)
    if task.task_type == "review":
        return cloud.reconcile_cloud_dispatch(
            queue, task, pr_comments_fn=lambda pr, repo=None: [])
    return cloud.reconcile_cloud_dispatch(
        queue, task,
        pr_number_for_branch_fn=lambda branch, repo=None: None,
        commit_message_fn=lambda branch, repo=None: None)


@pytest.mark.parametrize("role", ["reviewer", "implementer"])
def test_stale_cloud_task_ends_without_same_input_retry(queue, monkeypatch, role):
    context = ({"pr_number": 829, "reviewed_sha": "a" * 40}
               if role == "reviewer" else {})
    original = _dispatched(queue, f"{role}-829", role, context)
    first = _stale(queue, original, monkeypatch)
    assert first.action == "failed"
    assert queue.get_task(original.task_id).status == "failed"
    assert queue.count_in_progress_by_dispatch_channel(cloud.DISPATCH_CHANNEL) == 0
    assert not [t for t in queue.list_tasks() if t.task_id.startswith("retry-")]


@pytest.mark.parametrize("role", ["reviewer", "implementer"])
def test_non_stale_cloud_task_stays_dispatched(queue, monkeypatch, role):
    context = ({"pr_number": 829, "reviewed_sha": "a" * 40}
               if role == "reviewer" else {})
    task = _dispatched(queue, f"{role}-fresh", role, context)
    monkeypatch.setattr(queue, "get_dispatched_at", lambda _: time.time())
    if role == "reviewer":
        outcome = cloud.reconcile_cloud_dispatch(
            queue, task, pr_comments_fn=lambda pr, repo=None: [])
    else:
        outcome = cloud.reconcile_cloud_dispatch(
            queue, task,
            pr_number_for_branch_fn=lambda branch, repo=None: None,
            commit_message_fn=lambda branch, repo=None: None)
    assert outcome.action == "still_dispatched"
    assert queue.get_task(task.task_id).status == "in_progress"
    assert len(queue.list_tasks()) == 1
