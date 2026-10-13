"""CLI intent survives server-owned review, fix and test successors."""

import json
import sqlite3

import pytest

from fastapi.testclient import TestClient

from agent_crew.pipeline import auto_enqueue_fix, auto_enqueue_review, auto_enqueue_test
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.queue import LateResultRejected
from agent_crew.server import create_app
from agent_crew.claude_cloud import CloudReconciliationOutcome
from tests.contract.lineage_contract import assert_lineage_contract


INTENT = {"repo": "owner/repo", "issue": 773, "branch": "agent/773-work",
          "base_branch": "main", "coordinator_managed": True,
          "implementer": "codex", "reviewer": "claude",
          "role_map": {"implementer": "codex", "reviewer": "claude", "tester": "gemini"}}


def _root(queue):
    # The CLI client is gone after this durable enqueue; only the server paths
    # below are allowed to create successors.
    queue.enqueue(TaskRequest("impl-773", "implement", "do work",
                              branch=INTENT["branch"], context={
                                  "repo": INTENT["repo"], "issue": INTENT["issue"],
                                  "base_branch": "main", "coordinator_managed": True,
                                  "implementer_agent": "codex", "reviewer_agent": "claude",
                                  "agent_override": "codex"}))


def test_server_cascade_preserves_cli_intent_and_role_separation(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    queue = TaskQueue(tmp_db)
    _root(queue)
    queue.submit_result("impl-773", TaskResult("impl-773", "completed", "done", pr_number=773))
    review1 = auto_enqueue_review(queue, "impl-773", 773, pr_state_fn=lambda _: "open")
    assert review1
    queue.submit_result(review1, TaskResult(review1, "completed", "fix", verdict="request_changes",
                                            findings=["fix this"], pr_number=773))
    fix = auto_enqueue_fix(queue, review1, pr_state_fn=lambda _: "open", repo=INTENT["repo"])
    assert fix
    assert auto_enqueue_fix(queue, review1, pr_state_fn=lambda _: "open", repo=INTENT["repo"]) is None
    queue.submit_result(fix, TaskResult(fix, "completed", "fixed", pr_number=773))
    review2 = auto_enqueue_review(queue, fix, 773, pr_state_fn=lambda _: "open")
    assert review2
    queue.submit_result(review2, TaskResult(review2, "completed", "approved", verdict="approve",
                                            pr_number=773))
    assert auto_enqueue_test(queue, review2, pr_state_fn=lambda _: "open")
    lineage = assert_lineage_contract(tmp_db, "impl-773", INTENT)
    assert {task.task_type for task in lineage.values()} == {"implement", "review", "test"}
    assert len([task for task in lineage.values() if task.task_type == "implement"]) == 2


def test_http_refuses_override_with_no_running_role(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"role_agents": {"implementer": "claude_cloud",
                                                       "reviewer": "codex", "tester": "gemini"}}))
    app = create_app(tmp_db, pane_map={}, state_path=str(state),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks", json={"task_id": "unclaimable-773", "task_type": "review",
                     "description": "review", "branch": "agent/work",
                     "context": {"agent_override": "claude"}})
    assert response.status_code == 422
    assert "claude_cloud" in response.text and "codex" in response.text
    assert TaskQueue(tmp_db).get_task("unclaimable-773") is None


def test_http_refuses_unclaimable_future_reviewer(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"role_agents": {"implementer": "claude_cloud",
                                                       "reviewer": "codex", "tester": "gemini"}}))
    app = create_app(tmp_db, pane_map={}, state_path=str(state),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks", json={"task_id": "future-reviewer-773",
                     "task_type": "implement", "description": "implement", "branch": "agent/work",
                     "context": {"agent_override": "codex", "implementer_agent": "codex",
                                 "reviewer_agent": "claude"}})
    assert response.status_code == 422
    assert "reviewer_agent" in response.text and "role map" in response.text
    assert TaskQueue(tmp_db).get_task("future-reviewer-773") is None


def test_stalled_dispatch_notifies_once_and_fences_late_result(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    queue = TaskQueue(tmp_db)
    _root(queue)
    with sqlite3.connect(tmp_db) as db:
        db.execute("UPDATE tasks SET status='in_progress', claim_source='dispatcher', "
                   "last_activity_at=1000, dispatched_at=1000, "
                   "dispatch_agent='codex', dispatch_target='pid:123' WHERE task_id='impl-773'")
    notices = []
    app = create_app(tmp_db, pane_map={}, timeout_seconds=900,
                     watchdog_disabled=True, anomaly_disabled=True,
                     coordinator_notify_fn=lambda message: notices.append(message) or True)
    with TestClient(app):
        assert app.state.watchdog_tick(1901)["timed_out"] == ["impl-773"]
        app.state.watchdog_tick(2000)
    assert len(notices) == 1
    assert "impl-773" in notices[0] and "agent/773-work" in notices[0]
    with pytest.raises(LateResultRejected):
        queue.submit_result("impl-773", TaskResult("impl-773", "completed", "late"))


def test_cloud_stale_notifies_once_with_session_url(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    queue = TaskQueue(tmp_db)
    _root(queue)
    queue.patch_context("impl-773", {"cloud_session_url": "https://example.test/session/773"})
    with sqlite3.connect(tmp_db) as db:
        db.execute("UPDATE tasks SET status='in_progress', claim_source='dispatcher', "
                   "last_activity_at=1000, dispatched_at=1000, "
                   "dispatch_channel='claude_cloud', dispatch_agent='claude_cloud' "
                   "WHERE task_id='impl-773'")

    def reconcile(q, **_kwargs):
        if q.get_task_status("impl-773") == "in_progress":
            q.submit_result("impl-773", TaskResult("impl-773", "needs_human", "stale"))
            return [CloudReconciliationOutcome("impl-773", "needs_decision", detail="stale")]
        return []

    monkeypatch.setattr("agent_crew.server._claude_cloud.reconcile_all_cloud_tasks", reconcile)
    notices = []
    app = create_app(tmp_db, pane_map={}, watchdog_disabled=True, anomaly_disabled=True,
                     coordinator_notify_fn=lambda message: notices.append(message) or True)
    with TestClient(app):
        app.state.watchdog_tick(20000)
        app.state.watchdog_tick(20001)
    assert len(notices) == 1
    assert "impl-773" in notices[0]
    assert "https://example.test/session/773" in notices[0]


def test_server_retry_keeps_both_role_choices(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_RETRY_IMPLEMENT_SELF_FAILED", "1")
    queue = TaskQueue(tmp_db)
    _root(queue)
    app = create_app(tmp_db, pane_map={}, watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks/impl-773/result", json={
            "task_id": "impl-773", "status": "failed", "summary": "usage limit hit",
            "findings": [],
        })
    assert response.status_code == 200, response.text
    retry = queue.get_task("retry-impl-773-a1")
    assert retry is not None
    assert retry.context["agent_override"] == "codex"
    assert retry.context["reviewer_agent"] == "claude"
    assert retry.context["original_task_id"] == "impl-773"
    assert_lineage_contract(tmp_db, "impl-773", INTENT)
