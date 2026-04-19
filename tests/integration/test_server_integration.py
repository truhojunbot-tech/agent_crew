import threading

import pytest
from fastapi.testclient import TestClient

from agent_crew.server import create_app


pytestmark = pytest.mark.integration


# I-SV01: POST /tasks → GET /tasks/{id} — task 생성 및 조회
def test_i_sv01_create_and_get_task(test_client):
    payload = {
        "task_id": "sv01-task",
        "task_type": "implement",
        "description": "Build login",
        "branch": "feat/login",
        "priority": 3,
        "context": {},
    }
    resp = test_client.post("/tasks", json=payload)
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]

    resp = test_client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] == task_id
    assert data["task_type"] == "implement"


# I-SV02: POST /tasks → GET /tasks/next → POST /tasks/{id}/result — 전체 lifecycle
def test_i_sv02_full_lifecycle(test_client):
    payload = {
        "task_id": "sv02-task",
        "task_type": "implement",
        "description": "Implement auth",
        "branch": "feat/auth",
        "priority": 2,
        "context": {},
    }
    test_client.post("/tasks", json=payload)

    resp = test_client.get("/tasks/next", params={"role": "coder"})
    assert resp.status_code == 200
    task = resp.json()
    assert task is not None
    task_id = task["task_id"]

    result_payload = {
        "task_id": task_id,
        "status": "completed",
        "summary": "Auth implemented",
        "findings": [],
    }
    resp = test_client.post(f"/tasks/{task_id}/result", json=result_payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# I-SV03: GET /tasks/next concurrent (2 independent clients) — 중복 할당 없음
def test_i_sv03_concurrent_dequeue(tmp_db):
    app = create_app(tmp_db)
    with TestClient(app) as setup_client:
        for i in range(2):
            setup_client.post("/tasks", json={
                "task_id": f"sv03-task-{i}",
                "task_type": "implement",
                "description": f"Task {i}",
                "branch": "main",
                "priority": 3,
                "context": {},
            })

    results = []
    errors = []
    barrier = threading.Barrier(2)

    def dequeue():
        try:
            with TestClient(app) as client:
                barrier.wait()
                resp = client.get("/tasks/next", params={"role": "coder"})
                results.append(resp.json())
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=dequeue)
    t2 = threading.Thread(target=dequeue)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors
    non_null = [r for r in results if r is not None]
    task_ids = [r["task_id"] for r in non_null]
    assert len(task_ids) == len(set(task_ids)), "duplicate task assignment detected"


# I-SV04: GET /tasks?status=pending — 정확한 필터링
def test_i_sv04_list_tasks_filter(test_client):
    test_client.post("/tasks", json={
        "task_id": "sv04-pending",
        "task_type": "implement",
        "description": "Pending task",
        "branch": "main",
        "priority": 3,
        "context": {},
    })

    resp = test_client.get("/tasks", params={"status": "pending"})
    assert resp.status_code == 200
    tasks = resp.json()
    assert all(t["task_id"] for t in tasks)
    assert any(t["task_id"] == "sv04-pending" for t in tasks)


# I-SV05: DELETE /tasks/{id} — cancelled 처리, next에서 반환 안 됨
def test_i_sv05_cancel_task(test_client):
    test_client.post("/tasks", json={
        "task_id": "sv05-cancel",
        "task_type": "implement",
        "description": "To be cancelled",
        "branch": "main",
        "priority": 1,
        "context": {},
    })

    resp = test_client.delete("/tasks/sv05-cancel")
    assert resp.status_code == 200

    resp = test_client.get("/tasks/next", params={"role": "coder"})
    task = resp.json()
    assert task is None or task.get("task_id") != "sv05-cancel"


# I-SV06: POST /gates → GET /gates/pending — gate pending 목록
def test_i_sv06_create_gate_pending(test_client):
    gate_payload = {
        "id": "gate-sv06",
        "type": "approval",
        "message": "Please approve the PR",
        "status": "pending",
        "created_at": 0.0,
    }
    resp = test_client.post("/gates", json=gate_payload)
    assert resp.status_code == 201
    assert resp.json()["gate_id"] == "gate-sv06"

    resp = test_client.get("/gates/pending")
    assert resp.status_code == 200
    gates = resp.json()
    assert any(g["id"] == "gate-sv06" for g in gates)


# I-SV07: POST /gates/{id}/resolve → GET /gates/{id} — status 업데이트
def test_i_sv07_resolve_gate(test_client, resolve_approved):
    test_client.post("/gates", json={
        "id": "gate-sv07",
        "type": "approval",
        "message": "Approve deploy",
        "status": "pending",
        "created_at": 0.0,
    })

    resp = test_client.post("/gates/gate-sv07/resolve", json=resolve_approved)
    assert resp.status_code == 200

    resp = test_client.get("/gates/gate-sv07")
    assert resp.status_code == 200
    gate = resp.json()
    assert gate["status"] == "approved"


# I-SV07b: POST /gates/{id}/resolve {"status": "rejected"} → status=rejected
def test_i_sv07b_resolve_gate_rejected(test_client, resolve_rejected):
    test_client.post("/gates", json={
        "id": "gate-sv07b",
        "type": "approval",
        "message": "Reject deploy",
        "status": "pending",
        "created_at": 0.0,
    })

    resp = test_client.post("/gates/gate-sv07b/resolve", json=resolve_rejected)
    assert resp.status_code == 200

    resp = test_client.get("/gates/gate-sv07b")
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
