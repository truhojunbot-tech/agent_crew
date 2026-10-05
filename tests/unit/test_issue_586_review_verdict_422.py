"""#586: a review result without a verdict is refused (422), not marked done."""

import pytest
from fastapi.testclient import TestClient

from agent_crew.mcp_server import build_mcp_server
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


SUMMARY = "PR #586 review: verdict request_changes, one HIGH finding below."
FINDING = "HIGH src/agent_crew/server.py:10 - Intake: Reject a verdict-less review"


def _review(queue):
    queue.enqueue(TaskRequest(task_id="review-586", task_type="review",
                              description="Review PR #586", context={"pr_number": 586}))
    assert queue.dequeue(role="reviewer").task_id == "review-586"


def _task(queue, task_id):
    return next(task for task in queue.list_tasks() if task.task_id == task_id)


def test_missing_verdict_is_422_and_repost_with_verdict_completes(tmp_db, monkeypatch):
    queue = TaskQueue(tmp_db)
    _review(queue)
    monkeypatch.setattr("agent_crew.github.pr_state", lambda *args, **kwargs: "closed")
    payload = {"task_id": "review-586", "status": "completed", "summary": SUMMARY,
               "findings": [FINDING], "pr_number": 586}
    with TestClient(create_app(tmp_db)) as client:
        response = client.post("/tasks/review-586/result", json=payload)
        assert response.status_code == 422
        assert "verdict" in response.json()["detail"]
        assert _task(queue, "review-586").status == "in_progress"

        response = client.post("/tasks/review-586/result",
                               json={**payload, "verdict": "request_changes"})
    assert response.status_code == 200
    task = _task(queue, "review-586")
    assert task.status == "completed"
    assert task.verdict == "request_changes"


def test_invalid_verdict_shape_is_422(tmp_db):
    # approve carrying findings is not a verdict either (_resolve_verdict).
    queue = TaskQueue(tmp_db)
    _review(queue)
    with TestClient(create_app(tmp_db)) as client:
        response = client.post("/tasks/review-586/result", json={
            "task_id": "review-586", "status": "completed", "summary": SUMMARY,
            "verdict": "approve", "findings": [FINDING], "pr_number": 586,
        })
    assert response.status_code == 422
    assert "verdict" in response.json()["detail"]
    assert _task(queue, "review-586").status == "in_progress"


def test_pending_review_without_verdict_is_422_and_stays_pending(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id="review-586", task_type="review",
                              description="Review PR #586", context={"pr_number": 586}))
    with TestClient(create_app(tmp_db)) as client:
        response = client.post("/tasks/review-586/result", json={
            "task_id": "review-586", "status": "completed", "summary": SUMMARY,
            "findings": [FINDING], "pr_number": 586,
        })
    assert response.status_code == 422
    assert "verdict" in response.json()["detail"]
    assert _task(queue, "review-586").status == "pending"


@pytest.mark.parametrize("claimed", [False, True])
def test_mcp_review_without_verdict_is_rejected_and_stays_live(tmp_db, claimed):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id="review-586", task_type="review",
                              description="Review PR #586", context={"pr_number": 586}))
    if claimed:
        assert queue.dequeue(role="reviewer").task_id == "review-586"
    mcp = build_mcp_server(tmp_db)
    submit = mcp._tool_manager._tools["submit_result"].fn
    response = submit(task_id="review-586", status="completed", summary=SUMMARY,
                      findings=[FINDING], pr_number=586)
    assert response["acknowledged"] is False
    assert "verdict" in response["error"]
    assert _task(queue, "review-586").status == ("in_progress" if claimed else "pending")
