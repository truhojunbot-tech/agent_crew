"""
E2E tests for gate approval/rejection/timeout flows.

E-GA01: Gate created → resolve approved via API → gate removed from pending, loop continues
E-GA02: Gate created → resolve rejected via API → gate rejected, no implement task enqueued
E-GA03: Gate timeout → check_gate_timeout auto-rejects, removed from pending
E-GA04: Multiple pending gates → all listed via GET /gates/pending; resolving one removes only it
"""

import pytest
from fastapi.testclient import TestClient

import agent_crew.triage as triage_mod
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app
from agent_crew.triage import check_gate_timeout, enqueue_task

pytestmark = pytest.mark.e2e


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "gates.db")


@pytest.fixture
def task_queue(tmp_db):
    return TaskQueue(tmp_db)


@pytest.fixture
def test_client(tmp_db):
    app = create_app(tmp_db)
    with TestClient(app) as client:
        yield client


def _post_gate(client: TestClient, gate_id: str, gate_type: str = "approval",
               message: str = "test gate") -> str:
    resp = client.post("/gates", json={"id": gate_id, "type": gate_type, "message": message})
    assert resp.status_code == 201
    return resp.json()["gate_id"]


# E-GA01: approve via API → gate leaves pending, downstream task enqueued
def test_e_ga01_gate_approve_loop_continues(task_queue, test_client):
    gate_id = _post_gate(test_client, "gate-ga01", message="Approve and proceed")

    # Gate is pending
    resp = test_client.get("/gates/pending")
    assert gate_id in {g["id"] for g in resp.json()}

    # Approve via HTTP
    resp = test_client.post(f"/gates/{gate_id}/resolve", json={"status": "approved"})
    assert resp.status_code == 200

    # Gate no longer pending
    resp = test_client.get("/gates/pending")
    assert gate_id not in {g["id"] for g in resp.json()}

    # Gate status is approved
    resp = test_client.get(f"/gates/{gate_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"

    # Simulate loop continues: enqueue an implement task
    triage_result = {"parsed": {"issue": 1, "description": "Approved feature"}, "branch": "main"}
    task_id = enqueue_task(task_queue, triage_result)

    resp = test_client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["task_type"] == "implement"


# E-GA02: reject via API → gate rejected, no implement task enqueued
def test_e_ga02_gate_reject_skips_task(task_queue, test_client):
    gate_id = _post_gate(test_client, "gate-ga02", message="Reject and skip")

    # Reject via HTTP
    resp = test_client.post(f"/gates/{gate_id}/resolve", json={"status": "rejected"})
    assert resp.status_code == 200

    # Gate status is rejected
    resp = test_client.get(f"/gates/{gate_id}")
    assert resp.json()["status"] == "rejected"

    # Gate no longer in pending
    resp = test_client.get("/gates/pending")
    assert gate_id not in {g["id"] for g in resp.json()}

    # Loop skips: no implement task enqueued
    resp = test_client.get("/tasks")
    impl_tasks = [t for t in resp.json() if t["task_type"] == "implement"]
    assert len(impl_tasks) == 0


# E-GA03: check_gate_timeout auto-rejects stale gate (time mock)
def test_e_ga03_gate_timeout_auto_rejected(task_queue, test_client, monkeypatch):
    gate_id = _post_gate(test_client, "gate-ga03", message="Stale gate")

    # Get gate's created_at from server
    resp = test_client.get(f"/gates/{gate_id}")
    created_at = resp.json()["created_at"]

    timeout_secs = 3600
    # Mock time.time in triage module to be past the deadline
    monkeypatch.setattr(triage_mod.time, "time", lambda: created_at + timeout_secs + 1)

    rejected = check_gate_timeout(task_queue, timeout_seconds=timeout_secs)

    assert gate_id in rejected

    # Gate is now rejected in DB (visible via HTTP)
    resp = test_client.get(f"/gates/{gate_id}")
    assert resp.json()["status"] == "rejected"

    # Not in pending anymore
    resp = test_client.get("/gates/pending")
    assert gate_id not in {g["id"] for g in resp.json()}


# E-GA04: multiple pending gates → all listed; resolving one only removes that one
def test_e_ga04_multiple_pending_gates(test_client):
    ids = [
        _post_gate(test_client, f"gate-ga04-{i}", message=f"Gate {i}")
        for i in range(3)
    ]

    # All 3 are pending
    resp = test_client.get("/gates/pending")
    pending_ids = {g["id"] for g in resp.json()}
    assert set(ids) == pending_ids

    # Resolve only the first one
    resp = test_client.post(f"/gates/{ids[0]}/resolve", json={"status": "approved"})
    assert resp.status_code == 200

    # Pending now has exactly the remaining 2
    resp = test_client.get("/gates/pending")
    pending_after = {g["id"] for g in resp.json()}
    assert ids[0] not in pending_after
    assert ids[1] in pending_after
    assert ids[2] in pending_after
    assert len(pending_after) == 2
