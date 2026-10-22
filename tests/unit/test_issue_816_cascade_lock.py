"""#816: a busy tasks database must not silently end a result cascade."""

import sqlite3

import pytest

from agent_crew.pipeline import auto_enqueue_fix, auto_enqueue_review, auto_enqueue_test
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


PR = 999816


def _parent(queue, stage):
    task_type = "implement" if stage == "review" else "review"
    task_id = f"{task_type}-816-{stage}"
    queue.enqueue(TaskRequest(
        task_id=task_id, task_type=task_type, description="cascade parent",
        branch="agent/816-cascade-lock", project="agent_crew",
        context={"pr_number": PR, "repo": "owner/repo", "risk_tier": 1},
    ))
    fields = ({"verdict": "request_changes", "findings": ["fix the lock"]}
              if stage == "fix" else {"verdict": "approve"} if stage == "test" else {})
    queue.submit_result(task_id, TaskResult(
        task_id=task_id, status="completed", summary="parent completed",
        pr_number=PR, **fields,
    ))
    return task_id


def _cascade(queue, stage, parent):
    if stage == "review":
        return auto_enqueue_review(queue, parent, PR, pr_state_fn=lambda _: "open")
    if stage == "fix":
        return auto_enqueue_fix(queue, parent, repo="owner/repo",
                                pr_state_fn=lambda _: "open")
    return auto_enqueue_test(queue, parent, pr_state_fn=lambda _: "open")


@pytest.mark.parametrize("stage", ["review", "fix", "test"])
def test_first_locked_enqueue_retries_to_one_successor(tmp_path, monkeypatch, stage):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    parent = _parent(queue, stage)
    real_enqueue = queue.enqueue
    calls = []

    def lock_once(task, *args, **kwargs):
        calls.append(task.task_id)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_enqueue(task, *args, **kwargs)

    monkeypatch.setattr(queue, "enqueue", lock_once)
    successor = _cascade(queue, stage, parent)
    assert successor is not None
    assert calls == [successor, successor]
    assert len([task for task in queue.list_tasks() if task.task_id != parent]) == 1
    assert _cascade(queue, stage, parent) in (None, successor)
    assert len([task for task in queue.list_tasks() if task.task_id != parent]) == 1
    assert "cascade_enqueue_failure" not in queue.get_task(parent).context


@pytest.mark.parametrize("stage", ["review", "fix", "test"])
def test_persistent_locked_enqueue_records_one_notice(tmp_path, monkeypatch, stage):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    parent = _parent(queue, stage)
    calls, notices = [], []

    def locked(task, *args, **kwargs):
        calls.append(task.task_id)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(queue, "enqueue", locked)
    monkeypatch.setattr("agent_crew.notify.notify_telegram",
                        lambda message: notices.append(message) or True)
    assert _cascade(queue, stage, parent) is None
    assert _cascade(queue, stage, parent) is None
    assert len(calls) == 6  # three bounded attempts per cascade invocation
    assert not [task for task in queue.list_tasks() if task.task_id != parent]
    failure = queue.get_task(parent).context["cascade_enqueue_failure"]
    assert failure["missing_step"] == stage
    assert failure["reason"] == "database is locked"
    assert failure["lineage_task_id"] == parent
    assert len(notices) == 1
    assert parent in notices[0] and stage in notices[0]
