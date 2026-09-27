"""Review intake must not turn a diagnostic POST into a fix round."""

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import CompletedReviewRejected, TaskQueue
from agent_crew.server import create_app


SUMMARY = "PR #425 review found an actionable result intake defect."
FINDING = "HIGH src/agent_crew/server.py:10 - Intake: Reject a diagnostic result"


def _review(queue):
    queue.enqueue(TaskRequest(task_id="review-425", task_type="review",
                              description="Review PR #425", context={"pr_number": 425}))
    assert queue.dequeue(role="reviewer").task_id == "review-425"


def _task(queue, task_id):
    return next(task for task in queue.list_tasks() if task.task_id == task_id)


def test_structured_finding_is_normalized():
    result = TaskResult(task_id="review-425", status="completed", summary=SUMMARY,
                        verdict="request_changes", findings=[{
                            "severity": "HIGH", "file": "server.py", "line": 12,
                            "title": "Intake", "detail": "Reject diagnostic reviews",
                        }])
    assert result.findings == ["HIGH server.py:12 - Intake: Reject diagnostic reviews"]


def test_http_accepts_structured_finding(tmp_db, monkeypatch):
    queue = TaskQueue(tmp_db)
    _review(queue)
    monkeypatch.setattr("agent_crew.github.pr_state", lambda *args, **kwargs: "closed")
    with TestClient(create_app(tmp_db)) as client:
        response = client.post("/tasks/review-425/result", json={
            "task_id": "review-425", "status": "completed", "summary": SUMMARY,
            "verdict": "request_changes", "pr_number": 425,
            "findings": [{"severity": "HIGH", "file": "server.py", "line": 12,
                          "title": "Intake", "detail": "Reject diagnostic reviews"}],
        })
    assert response.status_code == 200
    assert _task(queue, "review-425").findings == [
        "HIGH server.py:12 - Intake: Reject diagnostic reviews"]


def test_probe_rejected_without_completing_task(tmp_db):
    queue = TaskQueue(tmp_db)
    _review(queue)
    with TestClient(create_app(tmp_db)) as client:
        response = client.post("/tasks/review-425/result", json={
            "task_id": "review-425", "status": "completed", "summary": "probe",
            "verdict": "request_changes", "findings": ["x"], "pr_number": 425,
        })
    assert response.status_code == 422
    assert "summary" in response.json()["detail"]
    assert _task(queue, "review-425").status == "in_progress"


def test_short_finding_rejected_with_clear_reason(tmp_db):
    queue = TaskQueue(tmp_db)
    _review(queue)
    with TestClient(create_app(tmp_db)) as client:
        response = client.post("/tasks/review-425/result", json={
            "task_id": "review-425", "status": "completed", "summary": SUMMARY,
            "verdict": "request_changes", "findings": ["x"], "pr_number": 425,
        })
    assert response.status_code == 422
    assert "findings[0]" in response.json()["detail"]
    assert _task(queue, "review-425").status == "in_progress"


def test_completed_review_cannot_replace_result_with_pending_fix(tmp_db):
    queue = TaskQueue(tmp_db)
    _review(queue)
    queue.submit_result("review-425", TaskResult(
        task_id="review-425", status="completed", summary=SUMMARY,
        verdict="request_changes", findings=[FINDING], pr_number=425))
    queue.enqueue(TaskRequest(task_id="fix-425", task_type="implement",
                              description=FINDING, context={"prev_task_id": "review-425"}))

    with pytest.raises(CompletedReviewRejected):
        queue.submit_result("review-425", TaskResult(
            task_id="review-425", status="completed", summary=SUMMARY,
            verdict="approve", findings=[], pr_number=425))

    review = _task(queue, "review-425")
    assert review.verdict == "request_changes"
    assert review.findings == [FINDING]
    assert _task(queue, "fix-425").status == "pending"

    with TestClient(create_app(tmp_db)) as client:
        response = client.post("/tasks/review-425/result", json={
            "task_id": "review-425", "status": "completed", "summary": SUMMARY,
            "verdict": "approve", "findings": [], "pr_number": 425,
        })
    assert response.status_code == 409
    assert _task(queue, "review-425").verdict == "request_changes"
