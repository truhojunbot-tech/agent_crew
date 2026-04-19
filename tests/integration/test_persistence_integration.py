import pytest
from fastapi.testclient import TestClient

from agent_crew.server import create_app


pytestmark = pytest.mark.integration


def _db(tmp_path):
    return str(tmp_path / "persist.db")


# I-PS01: tasks enqueued → app recreated → still pending
def test_i_ps01_tasks_survive_restart(tmp_path):
    db = _db(tmp_path)

    with TestClient(create_app(db)) as client:
        for i in range(2):
            client.post("/tasks", json={
                "task_id": f"ps01-task-{i}",
                "task_type": "implement",
                "description": f"Task {i}",
                "branch": "main",
                "priority": 3,
                "context": {},
            })

    with TestClient(create_app(db)) as client:
        resp = client.get("/tasks", params={"status": "pending"})
        assert resp.status_code == 200
        ids = {t["task_id"] for t in resp.json()}
        assert "ps01-task-0" in ids
        assert "ps01-task-1" in ids


# I-PS02: dequeued task (in_progress) → app recreated → status still in_progress
def test_i_ps02_in_progress_survives_restart(tmp_path):
    db = _db(tmp_path)

    with TestClient(create_app(db)) as client:
        client.post("/tasks", json={
            "task_id": "ps02-task",
            "task_type": "implement",
            "description": "In-progress task",
            "branch": "main",
            "priority": 2,
            "context": {},
        })
        resp = client.get("/tasks/next", params={"role": "coder"})
        assert resp.json() is not None

    with TestClient(create_app(db)) as client:
        resp = client.get("/tasks", params={"status": "in_progress"})
        assert resp.status_code == 200
        ids = {t["task_id"] for t in resp.json()}
        assert "ps02-task" in ids


# I-PS03: completed tasks (submit_result) → app recreated → queryable
def test_i_ps03_completed_survives_restart(tmp_path):
    db = _db(tmp_path)

    with TestClient(create_app(db)) as client:
        client.post("/tasks", json={
            "task_id": "ps03-task",
            "task_type": "implement",
            "description": "Task to complete",
            "branch": "main",
            "priority": 2,
            "context": {},
        })
        client.get("/tasks/next", params={"role": "coder"})
        client.post("/tasks/ps03-task/result", json={
            "task_id": "ps03-task",
            "status": "completed",
            "summary": "Done",
            "findings": [],
        })

    with TestClient(create_app(db)) as client:
        resp = client.get("/tasks", params={"status": "completed"})
        assert resp.status_code == 200
        ids = {t["task_id"] for t in resp.json()}
        assert "ps03-task" in ids


# I-PS04: pending gate → app recreated → GET /gates/pending still includes it
def test_i_ps04_gates_survive_restart(tmp_path):
    db = _db(tmp_path)

    with TestClient(create_app(db)) as client:
        client.post("/gates", json={
            "id": "gate-ps04",
            "type": "approval",
            "message": "Please approve",
            "status": "pending",
            "created_at": 0.0,
        })

    with TestClient(create_app(db)) as client:
        resp = client.get("/gates/pending")
        assert resp.status_code == 200
        ids = {g["id"] for g in resp.json()}
        assert "gate-ps04" in ids
