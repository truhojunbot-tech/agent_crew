"""A late original result and its provider fallback share one review lineage."""

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue, _CEA_SYSTEM_SUCCESSOR_PROVENANCE
from agent_crew.server import create_app


def _prepare(db, fallback_status=None, original_status="failed"):
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest(task_id="impl-root", task_type="implement",
                              description="work", branch="feature"))
    assert queue.dequeue(role="implementer")
    queue.submit_result("impl-root", TaskResult(
        task_id="impl-root", status=original_status, summary="wrapper failed"))
    if fallback_status is not None:
        queue.enqueue(TaskRequest(
            task_id="fallback-impl-root-d1", task_type="implement",
            description="work", branch="feature",
            context={"original_task_id": "impl-root",
                     "fallback_from_task_id": "impl-root", "fallback_chain_depth": 1},
        ), _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
        if fallback_status in ("in_progress", "completed", "failed", "timed_out"):
            assert queue.dequeue(role="implementer")
        if fallback_status in ("completed", "failed", "timed_out"):
            queue.submit_result("fallback-impl-root-d1", TaskResult(
                task_id="fallback-impl-root-d1", status=fallback_status,
                summary="fallback done"))
        if fallback_status == "cancelled":
            assert queue.cancel("fallback-impl-root-d1")
    return queue


def _late_post(db):
    app = create_app(db, watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        return client.post("/tasks/impl-root/result", json={
            "task_id": "impl-root", "status": "completed", "summary": "original finished",
            "commit": "a" * 40,
        })


@pytest.mark.parametrize("fallback_status", ["pending", "in_progress"])
@pytest.mark.parametrize("original_status", ["failed", "timed_out"])
def test_unfinished_fallback_is_cancelled_before_original_review(
        tmp_db, fallback_status, original_status):
    queue = _prepare(tmp_db, fallback_status, original_status)
    response = _late_post(tmp_db)
    assert response.status_code == 200, response.text
    assert response.json().get("status") != "ignored_late_result"
    assert queue.get_task_status("impl-root") == "completed"
    assert queue.get_task_status("fallback-impl-root-d1") == "cancelled"
    assert len([t for t in queue.list_tasks() if t.task_type == "review"]) == 1


@pytest.mark.parametrize("fallback_status", ["completed", "failed", "timed_out", "cancelled"])
def test_finished_fallback_keeps_original_failed_and_audits_late_result(tmp_db, fallback_status):
    queue = _prepare(tmp_db, fallback_status)
    response = _late_post(tmp_db)
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ignored_late_result",
                               "fallback_task_id": "fallback-impl-root-d1"}
    assert queue.get_task_status("impl-root") == "failed"
    assert queue.get_task_status("fallback-impl-root-d1") == fallback_status
    assert not [t for t in queue.list_tasks() if t.task_type == "review"]
    late = [event for event in queue.get_exec_state("impl-root")["events"]
            if event["event"] == "late_result"]
    assert len(late) == 1
    assert late[0]["summary"] == "original finished"


def test_late_original_without_fallback_keeps_existing_revision(tmp_db):
    queue = _prepare(tmp_db)
    response = _late_post(tmp_db)
    assert response.status_code == 200, response.text
    assert response.json().get("status") != "ignored_late_result"
    assert queue.get_task_status("impl-root") == "completed"
    assert len([t for t in queue.list_tasks() if t.task_type == "review"]) == 1
