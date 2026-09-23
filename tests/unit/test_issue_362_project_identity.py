"""Regression coverage for #362: a recycled port must not cross crew projects."""

import logging
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient

from agent_crew.server import create_app


def _client(tmp_path, project="council"):
    return TestClient(create_app(
        str(tmp_path / "tasks.db"), project=project, port=8102,
        watchdog_disabled=True, anomaly_disabled=True, identity_required=True,
    ))


def _result():
    return {"task_id": "foreign-task", "status": "completed", "summary": "finished", "findings": []}


def test_health_reports_project_identity(tmp_path):
    with _client(tmp_path, project="apify-forge") as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["project"] == "apify-forge"


def test_worker_protocol_requires_health_identity_check_and_project_header():
    from agent_crew.instructions import generate

    protocol = generate("implementer", "agent_council", 8102, delivery="push")

    assert "/health" in protocol
    assert "X-Agent-Crew-Project: agent_council" in protocol
    assert "Do not receive a task or submit a result" in protocol


def test_foreign_project_header_cannot_receive_task_or_submit_result(tmp_path, caplog):
    with _client(tmp_path, project="apify-forge") as client:
        client.post("/tasks", json={
            "task_id": "foreign-task", "task_type": "implement", "description": "foreign work",
        })
        with caplog.at_level(logging.ERROR, logger="agent_crew.server"):
            received = client.get("/tasks/next?role=implementer", headers={
                "X-Agent-Crew-Project": "agent_council",
            })
            submitted = client.post("/tasks/foreign-task/result", json=_result(), headers={
                "X-Agent-Crew-Project": "agent_council",
            })

    assert received.status_code == 409
    assert submitted.status_code == 409
    assert "agent_council" in received.json()["detail"]
    assert "apify-forge" in received.json()["detail"]
    assert "expected project='agent_council'" in caplog.text
    assert "server project='apify-forge'" in caplog.text


def test_same_project_header_allows_task_receive_and_result_submission(tmp_path):
    with _client(tmp_path, project="agent_council") as client:
        client.post("/tasks", json={
            "task_id": "foreign-task", "task_type": "implement", "description": "own work",
        })
        received = client.get("/tasks/next?role=implementer", headers={
            "X-Agent-Crew-Project": "agent_council",
        })
        submitted = client.post("/tasks/foreign-task/result", json=_result(), headers={
            "X-Agent-Crew-Project": "agent_council",
        })

    assert received.status_code == 200
    assert received.json()["task_id"] == "foreign-task"
    assert submitted.status_code == 200


def test_identity_verification_blocks_when_health_endpoint_is_unavailable(monkeypatch, caplog):
    from agent_crew.project_identity import ProjectIdentityError, verify_server_identity

    def no_health(*_args, **_kwargs):
        raise HTTPError("http://127.0.0.1:8102/health", 404, "Not Found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", no_health)
    with caplog.at_level(logging.ERROR, logger="agent_crew.project_identity"):
        with pytest.raises(ProjectIdentityError, match="identity verification failed"):
            verify_server_identity("http://127.0.0.1:8102", "agent_council")

    assert "expected project='agent_council'" in caplog.text
    assert "identity verification failed" in caplog.text
