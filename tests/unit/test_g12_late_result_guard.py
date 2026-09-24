"""System-ended tasks retain late worker payloads without restarting a cascade."""

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


@pytest.mark.parametrize("ended", ["timed_out", "cancelled", "failed", "failed_result"])
def test_system_terminal_result_is_evidence_only(tmp_db, ended):
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="review-late", task_type="review",
                          description="review", branch="main"))
    assert q.dequeue(role="reviewer")
    if ended == "cancelled":
        assert q.cancel("review-late")
    elif ended == "failed":
        assert q.force_fail("review-late", "watchdog timeout") == "review"
    elif ended == "failed_result":
        q.submit_result("review-late", TaskResult(
            task_id="review-late", status="failed", summary="dispatcher exit",
            error_info={"reason": "exit_1", "final": True}))
    else:
        q.submit_result("review-late", TaskResult(
            task_id="review-late", status="timed_out", summary="dispatcher timeout"))
    before = next(t for t in q.list_tasks() if t.task_id == "review-late")
    before_outbox = q.outbox_get("review-late")
    app = create_app(tmp_db, watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks/review-late/result", json={
            "task_id": "review-late", "status": "completed", "summary": "late review",
            "verdict": "request_changes", "findings": ["fix this"],
            "commit": "a" * 40,
        })
    assert response.status_code == 409
    assert response.json()["late_result"] is True
    assert response.json()["accepted"] is False
    after = next(t for t in q.list_tasks() if t.task_id == "review-late")
    assert (after.status, after.verdict, after.summary, after.status_changed_at) == (
        before.status, before.verdict, before.summary, before.status_changed_at)
    assert [t.task_id for t in q.list_tasks()] == ["review-late"]
    assert q.outbox_get("review-late") == before_outbox
    late = [e for e in q.get_exec_state("review-late")["events"]
            if e["event"] == "late_result"]
    assert len(late) == 1
    assert (late[0]["prior_status"], late[0]["verdict"], late[0]["commit"]) == (
        "failed" if ended == "failed_result" else ended, "request_changes", "a" * 40)


def test_in_progress_result_still_completes(tmp_db):
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="review-live", task_type="review",
                          description="review", branch="main",
                          context={"coordinator_managed": True}))
    assert q.dequeue(role="reviewer")
    app = create_app(tmp_db, watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks/review-live/result", json={
            "task_id": "review-live", "status": "completed", "summary": "done",
            "verdict": "request_changes", "findings": ["fix this"],
        })
    assert response.status_code == 200
    task = next(t for t in q.list_tasks() if t.task_id == "review-live")
    assert (task.status, task.verdict, task.summary) == (
        "completed", "request_changes", "done")
    assert not [e for e in q.get_exec_state("review-live")["events"]
                if e["event"] == "late_result"]
