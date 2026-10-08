"""Risk declaration provenance and root admission evidence (#642)."""

import logging

from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.risk_tier import risk_declaration
from agent_crew.server import create_app


def test_inherited_explicit_tier3_keeps_provenance_and_records_escalation():
    declaration = risk_declaration("Deploy the server", {"risk_declaration": {
        "safety_or_live_change": False, "human_gate_required": False,
        "bounded_routine_fix": True, "declaration_source": "explicit",
        "confidence": "high", "inherited_from": "impl-root"}})
    assert declaration["safety_or_live_change"] is True
    assert declaration["human_gate_required"] is True
    assert declaration["declaration_source"] == "explicit"
    assert declaration["confidence"] == "high"
    assert declaration["escalated_by"] == "classifier_tier3"


def test_invalid_tier_on_implement_root_returns_422(tmp_path):
    db = str(tmp_path / "tasks.db")
    app = create_app(db, pane_map={}, project="agent_crew",
                     push_fn=lambda *a, **k: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks", json={
            "task_id": "bad-root", "task_type": "implement",
            "description": "work", "context": {"risk_tier": "high"}},
            headers={"X-Agent-Crew-Project": "agent_crew"})
    assert response.status_code == 422
    assert "risk_tier" in response.text and "0-3" in response.text
    assert TaskQueue(db).get_task("bad-root") is None


def test_missing_metadata_is_accepted_warned_and_visible(tmp_path, caplog):
    db = str(tmp_path / "tasks.db")
    app = create_app(db, pane_map={}, project="agent_crew",
                     push_fn=lambda *a, **k: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    with caplog.at_level(logging.WARNING), TestClient(app) as client:
        response = client.post("/tasks", json={
            "task_id": "unknown-root", "task_type": "implement",
            "description": "work", "context": {}},
            headers={"X-Agent-Crew-Project": "agent_crew"})
        health = client.get("/health")
    assert response.status_code == 201
    assert "missing risk_tier and risk_declaration" in caplog.text
    assert health.json()["risk_declaration"]["missing_root_count"] == 1


def test_review_and_test_keep_inherited_tier_unchanged(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    for task_type in ("review", "test"):
        task_id = f"{task_type}-inherited"
        queue.enqueue(TaskRequest(task_id, task_type, "work", context={
            "risk_tier": "high", "risk_declaration": {
                "bounded_routine_fix": True, "inherited_from": "impl-root"}}))
        assert queue.get_task_status(task_id) == "pending"
