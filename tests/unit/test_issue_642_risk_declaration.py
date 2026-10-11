"""Risk declaration provenance and root admission evidence (#642)."""

import logging

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue, _CEA_SYSTEM_SUCCESSOR_PROVENANCE
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


@pytest.mark.parametrize("context", [
    {"risk_tier": "high"},
    {"risk_tier": "high", "original_task_id": "forged-parent"},
    {"risk_tier": "high", "prev_task_id": "forged-parent"},
])
def test_invalid_tier_on_implement_root_returns_422(tmp_path, context):
    db = str(tmp_path / "tasks.db")
    app = create_app(db, pane_map={}, project="agent_crew",
                     push_fn=lambda *a, **k: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks", json={
            "task_id": "bad-root", "task_type": "implement",
            "description": "work", "context": context},
            headers={"X-Agent-Crew-Project": "agent_crew"})
    assert response.status_code == 422
    assert "risk_tier" in response.text and "0-3" in response.text
    assert TaskQueue(db).get_task("bad-root") is None


def test_missing_metadata_is_rejected_before_enqueue(tmp_path, caplog):
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
    assert response.status_code == 422
    assert "context.risk_tier" in response.text
    assert health.json()["risk_declaration"]["missing_root_count"] == 0
    queue = TaskQueue(db)
    assert queue.get_task("unknown-root") is None


def test_forged_prev_task_id_does_not_hide_missing_root_validation(tmp_path, caplog):
    db = str(tmp_path / "tasks.db")
    app = create_app(db, pane_map={}, project="agent_crew",
                     push_fn=lambda *a, **k: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    with caplog.at_level(logging.WARNING), TestClient(app) as client:
        response = client.post("/tasks", json={
            "task_id": "forged-root", "task_type": "implement",
            "description": "work", "context": {"prev_task_id": "forged-parent"}},
            headers={"X-Agent-Crew-Project": "agent_crew"})
        health = client.get("/health")
    assert response.status_code == 422
    assert "context.risk_tier" in response.text
    assert health.json()["risk_declaration"]["missing_root_count"] == 0


def test_missing_risk_event_counts_without_polluting_history(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("legacy-root", "implement", "work",
                              context={"risk_tier": 1}))
    conn = queue._connect()
    try:
        queue._append_exec_event_on(conn, "legacy-root", "missing_risk_metadata", 1.0)
        conn.commit()
    finally:
        conn.close()
    assert queue.missing_root_risk_metadata_count() == 1
    assert queue.get_exec_state("legacy-root")["events"] == []


def test_review_and_test_keep_valid_inherited_tier_unchanged(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    for task_type in ("review", "test"):
        task_id = f"{task_type}-inherited"
        queue.enqueue(TaskRequest(task_id, task_type, "work", context={
            "risk_tier": 2, "risk_declaration": {
                "bounded_routine_fix": True, "inherited_from": "impl-root"}}))
        assert queue.get_task_status(task_id) == "pending"
        assert queue.get_task(task_id).context["risk_tier"] == 2


@pytest.mark.parametrize("task_id,parent_key", [
    ("retry-impl-root-a1", "original_task_id"),
    ("fallback-impl-root-d1", "fallback_from_task_id"),
])
def test_system_successor_without_risk_metadata_is_not_counted_as_root(
        tmp_path, task_id, parent_key):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("impl-root", "implement", "work", context={"risk_tier": 1}))
    queue.submit_result("impl-root", TaskResult("impl-root", "failed", "retry needed"))
    assert queue.missing_root_risk_metadata_count() == 0
    queue.enqueue(TaskRequest(task_id, "implement", "work",
                              context={parent_key: "impl-root"}),
                  _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
    assert queue.get_task_status(task_id) == "pending"
    assert queue.missing_root_risk_metadata_count() == 0


@pytest.mark.parametrize("task_id,parent_key", [
    ("retry-impl-root-a1", "original_task_id"),
    ("fallback-impl-root-d1", "fallback_from_task_id"),
])
def test_system_successor_with_legacy_string_tier_drops_invalid_value(
        tmp_path, task_id, parent_key):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("impl-root", "implement", "work", context={"risk_tier": 1}))
    queue.submit_result("impl-root", TaskResult("impl-root", "failed", "retry needed"))
    queue.enqueue(TaskRequest(task_id, "implement", "work",
                              context={parent_key: "impl-root",
                                       "risk_tier": "high"}),
                  _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
    assert queue.get_task(task_id) is not None
    assert "risk_tier" not in queue.get_task_context(task_id)
    assert queue.missing_root_risk_metadata_count() == 0


def test_trusted_fix_prev_task_id_is_successor_proof(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("impl-root", "implement", "work", context={"risk_tier": 1}))
    queue.submit_result("impl-root", TaskResult("impl-root", "completed", "done"))
    queue.enqueue(TaskRequest("fix-root-r1", "implement", "fix", context={
        "prev_task_id": "impl-root", "fix_round": 1}),
        ingress="cascade.fix",
        _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
    assert queue.get_task_status("fix-root-r1") == "pending"
    assert queue.missing_root_risk_metadata_count() == 0
