"""Issue #213: GET /tasks and GET /tasks/{id} dropped summary/verdict/
findings/pr_number/error_info even though the DB row had them — a caller
polling the HTTP API for a completed task's result saw only the request
shape (task_id, task_type, description, ..., status) and read that as "the
result was lost," then re-ran the same work.

Root cause: TaskQueue.list_tasks() did `SELECT *` (every column, including
the result fields) but only used a subset of the row to build a TaskRequest,
which had no fields for the rest — silently discarding them. GET /tasks/{id}
and GET /tasks both go through list_tasks(), so both were affected.
"""
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _make_app(tmp_db):
    return create_app(db_path=tmp_db, watchdog_disabled=True)


def test_u213_list_tasks_includes_result_fields_after_completion(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(
        task_id="review-213a", task_type="review",
        description="review something", branch="main",
    ))
    queue.submit_result("review-213a", TaskResult(
        task_id="review-213a", status="completed",
        summary="Looks good, approve.", verdict="approve",
        findings=["minor: consider renaming x"], pr_number=42,
    ))

    task = next(t for t in queue.list_tasks() if t.task_id == "review-213a")
    assert task.summary == "Looks good, approve."
    assert task.verdict == "approve"
    assert task.findings == ["minor: consider renaming x"]
    assert task.pr_number == 42


def test_u213_list_tasks_includes_error_info_on_failure(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(
        task_id="test-213b", task_type="test",
        description="test something", branch="main",
    ))
    queue.submit_result("test-213b", TaskResult(
        task_id="test-213b", status="failed",
        summary="tests failed", error_info={"reason": "exit_1"},
    ))

    task = next(t for t in queue.list_tasks() if t.task_id == "test-213b")
    assert task.error_info == {"reason": "exit_1"}


def test_u213_list_tasks_defaults_are_empty_for_pending_task(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(
        task_id="impl-213c", task_type="implement",
        description="do something", branch="main",
    ))

    task = next(t for t in queue.list_tasks() if t.task_id == "impl-213c")
    assert task.summary == ""
    assert task.verdict is None
    assert task.findings == []
    assert task.pr_number is None
    assert task.error_info is None


def test_u213_get_task_http_endpoint_returns_result_fields(tmp_db, github_writes):
    """The actual reported symptom: polling GET /tasks/{id} over HTTP after
    completion must surface the result, not just the request shape."""
    app = _make_app(tmp_db)
    with TestClient(app) as client:
        client.post("/tasks", json={
            "task_id": "review-213d", "task_type": "review",
            "description": "review PR", "branch": "main",
            "priority": 3, "context": {}, "project": "",
        })
        client.post("/tasks/review-213d/result", json={
            "task_id": "review-213d", "status": "completed",
            "summary": "Approved — no blocking issues.",
            "verdict": "approve", "findings": [], "pr_number": 99,
        })

        resp = client.get("/tasks/review-213d")
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"] == "Approved — no blocking issues."
        assert body["verdict"] == "approve"
        assert body["pr_number"] == 99
        assert body["status"] == "completed"
