"""#883: a completed implement/test result is terminal.

Its cascade already ran (review enqueued, merge decided). A later POST that
flips the row to ``failed`` would auto-retry a second implement on the same
branch while the review continues. The late payload is kept as evidence only.
"""

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import LateResultRejected, TaskQueue
from agent_crew.server import create_app


def _complete(queue, task_id, task_type, role):
    queue.enqueue(TaskRequest(task_id=task_id, task_type=task_type,
                              description=f"#883 {task_type}", context={}))
    assert queue.dequeue(role=role).task_id == task_id
    queue.submit_result(task_id, TaskResult(task_id=task_id, status="completed",
                                            summary="done"))


def _late_events(queue, task_id):
    conn = queue._connect()
    try:
        return conn.execute(
            "SELECT event FROM task_exec_events WHERE task_id=? AND event='late_result'",
            (task_id,)).fetchall()
    finally:
        conn.close()


@pytest.mark.parametrize("task_type,role", [("implement", "implementer"),
                                            ("test", "tester")])
@pytest.mark.parametrize("new_status", ["failed", "timed_out", "needs_human", "blocked"])
def test_completed_task_cannot_change_status(tmp_db, task_type, role, new_status):
    queue = TaskQueue(tmp_db)
    task_id = f"{task_type}-883"
    _complete(queue, task_id, task_type, role)
    before = queue.outbox_get(task_id)

    with pytest.raises(LateResultRejected) as exc:
        queue.submit_result(task_id, TaskResult(task_id=task_id, status=new_status,
                                                summary="late failure"))

    assert exc.value.status == "completed"
    task = queue.get_task(task_id)
    assert task.status == "completed"
    assert task.summary == "done"
    assert queue.outbox_get(task_id) == before
    assert len(_late_events(queue, task_id)) == 1


def test_identical_completed_resubmit_still_accepted(tmp_db):
    queue = TaskQueue(tmp_db)
    _complete(queue, "implement-883", "implement", "implementer")
    assert queue.submit_result("implement-883", TaskResult(
        task_id="implement-883", status="completed", summary="done")) == "implement"


def test_http_late_failure_on_completed_implement_is_409(tmp_db):
    queue = TaskQueue(tmp_db)
    _complete(queue, "implement-883", "implement", "implementer")
    with TestClient(create_app(tmp_db)) as client:
        response = client.post("/tasks/implement-883/result", json={
            "task_id": "implement-883", "status": "failed", "summary": "late failure"})
    assert response.status_code == 409
    assert response.json()["prior_status"] == "completed"
    assert queue.get_task("implement-883").status == "completed"
    retries = [t for t in queue.list_tasks()
               if t.task_type == "implement" and t.task_id != "implement-883"]
    assert retries == []
