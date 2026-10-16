"""#801: terminal provider failures must not strand coordinator-managed lineages."""

import time

from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _harness(tmp_path, monkeypatch, *, task_type="test", retry_attempt=0):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    db = str(tmp_path / "tasks.db")
    notices = []
    app = create_app(db, project="agent_crew", pane_map={}, identity_required=False,
                     watchdog_disabled=True, anomaly_disabled=True,
                     coordinator_notify_fn=lambda message: notices.append(message) or True)
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest(
        task_id="root-801", task_type=task_type, description="provider task",
        branch="agent/801-failed-no-successor-notice", project="agent_crew",
        context={"coordinator_managed": True, "retry_attempt": retry_attempt,
                 "lineage_root_task_id": "root-801", "risk_tier": 2}),
        ingress="http.tasks")
    return app, queue, notices


def _fail(client, *, summary="provider exited", reason="exit_1", final=True):
    return client.post("/tasks/root-801/result", json={
        "task_id": "root-801", "status": "failed", "summary": summary,
        "findings": [], "error_info": {"reason": reason, "final": final},
    })


def test_exhausted_provider_tester_notifies_once_and_says_not_run(tmp_path, monkeypatch):
    app, queue, notices = _harness(tmp_path, monkeypatch, retry_attempt=2)
    with TestClient(app) as client:
        response = _fail(client, summary="gemini exit_1")
        assert response.status_code == 200, response.text
        app.state.watchdog_tick(time.time() + 30)
    assert queue.get_task("retry-root-801-a3") is None
    assert len(notices) == 1
    assert all(part in notices[0] for part in (
        "failed_no_successor", "root-801", "exit_1", "gemini exit_1",
        "test not run (provider)"))


def test_no_retry_terminal_failure_notifies_once(tmp_path, monkeypatch):
    app, queue, notices = _harness(tmp_path, monkeypatch, task_type="implement")
    monkeypatch.setenv("AGENT_CREW_RETRY_IMPLEMENT_SELF_FAILED", "0")
    with TestClient(app) as client:
        response = _fail(client, summary="agent stopped", reason="", final=True)
        assert response.status_code == 200, response.text
    assert queue.get_task("retry-root-801-a1") is None
    assert len(notices) == 1
    assert "failed_no_successor" in notices[0]
    assert "agent stopped" in notices[0]


def test_implement_retry_with_budget_has_no_notice(tmp_path, monkeypatch):
    app, queue, notices = _harness(tmp_path, monkeypatch, task_type="implement")
    with TestClient(app) as client:
        response = _fail(client)
        assert response.status_code == 200, response.text
    assert queue.get_task("retry-root-801-a1") is not None
    assert notices == []


def test_rate_limit_exhaustion_keeps_escalation_gate(tmp_path, monkeypatch):
    app, queue, notices = _harness(tmp_path, monkeypatch, task_type="implement",
                                   retry_attempt=2)
    with TestClient(app) as client:
        response = _fail(client, summary="provider 429 rate limit")
        assert response.status_code == 200, response.text
    assert queue.get_task("retry-root-801-a3") is None
    assert any(g.id == "escalation-root-801-provider-limit" for g in queue.list_gates())
    assert notices == []
