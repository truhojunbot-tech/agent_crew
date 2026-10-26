"""Completed tasks need causal provenance before a work intent can adopt them."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


OP_ID = "alfred44:causal-guard"
INTENT_AT = 2_000.0


@pytest.fixture
def crew(tmp_path):
    db = str(tmp_path / "tasks.db")
    app = create_app(db, project="alfred", pane_map={},
                     watchdog_disabled=True, anomaly_disabled=True,
                     identity_required=False)
    with TestClient(app) as client:
        yield client, TaskQueue(db)


def _admit(client, *, target=False):
    payload = {"external_operation_id": OP_ID, "project": "alfred",
               "repo": "owner/alfred", "issue": 44,
               "coordinator_identity": "alfred-owner",
               "coordinator_session": "session-1", "priority": 1,
               "risk_tier": 3, "execution_slo_s": 60}
    if target:
        payload.update(target_repo="owner/alfred", target_issue=405)
    response = client.post("/work-intents/admit", json=payload)
    assert response.status_code == 201


def _task(queue, *, task_id="test-405", issue=405, tier=2,
          status="completed", created_at=1_000.0, operation_id=None):
    context = {"issue": issue, "repo": "owner/alfred", "risk_tier": tier}
    if operation_id is not None:
        context["external_operation_id"] = operation_id
    queue.enqueue(TaskRequest(task_id=task_id, task_type="test",
                              description="Canonical test", branch="agent/405",
                              project="alfred", context=context))
    with sqlite3.connect(queue.db_path) as conn:
        conn.execute("UPDATE tasks SET status=?, created_at=? WHERE task_id=?",
                     (status, created_at, task_id))


def _set_intent_time(queue):
    with sqlite3.connect(queue.db_path) as conn:
        conn.execute("UPDATE work_intents SET received_at=? WHERE external_operation_id=?",
                     (INTENT_AT, OP_ID))


def _bind(client, task_id="test-405"):
    return client.post(f"/work-intents/{OP_ID}/bind", json={"task_id": task_id})


def test_older_completed_same_issue_task_cannot_finish_new_intent(crew):
    client, queue = crew
    _admit(client)
    _set_intent_time(queue)
    _task(queue, issue=44)
    before = queue.get_work_intent(OP_ID)

    response = _bind(client)

    assert response.status_code == 409
    assert "predates work intent" in response.json()["detail"]
    after = queue.get_work_intent(OP_ID)
    assert (after["state"], after["task_ids"]) == (before["state"], [])
    assert "external_operation_id" not in queue.get_task("test-405").context


def test_newer_completed_target_task_reconciles_with_lower_tier(crew):
    client, queue = crew
    _admit(client, target=True)
    _set_intent_time(queue)
    _task(queue, created_at=3_000.0)

    response = _bind(client)

    assert response.status_code == 200
    assert (response.json()["state"], response.json()["task_ids"]) == (
        "SUCCESS", ["test-405"])
    assert queue.get_task("test-405").context["risk_tier"] == 2
    assert queue.get_task("test-405").context["issue"] == 405


def test_older_completed_task_with_stored_operation_provenance_binds(crew):
    client, queue = crew
    _admit(client, target=True)
    _set_intent_time(queue)
    _task(queue, operation_id=OP_ID)

    response = _bind(client)

    assert response.status_code == 200
    assert response.json()["state"] == "SUCCESS"


@pytest.mark.parametrize("target, issue", [(False, 405), (True, 406)])
def test_wrong_issue_never_gains_completed_task_waiver(crew, target, issue):
    client, queue = crew
    _admit(client, target=target)
    _set_intent_time(queue)
    _task(queue, issue=issue, created_at=3_000.0)

    response = _bind(client)

    assert response.status_code == 409
    assert "issue" in response.json()["detail"]
    assert queue.get_work_intent(OP_ID)["task_ids"] == []


def test_older_pending_task_still_binds(crew):
    client, queue = crew
    _admit(client, target=True)
    _set_intent_time(queue)
    _task(queue, status="pending")

    response = _bind(client)

    assert response.status_code == 200
    assert response.json()["state"] == "BOUND"
