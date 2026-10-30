"""#801: terminal provider failures must not strand coordinator-managed lineages."""

import asyncio
import json
import pytest
pytestmark = [pytest.mark.usefixtures("legacy_gemini_tester_policy"),
              pytest.mark.usefixtures("pinned_task_transport")]
import time
from unittest.mock import AsyncMock, MagicMock

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


def _fail(client, *, summary="provider exited", reason="exit_1", final=True,
          kind=None):
    return client.post("/tasks/root-801/result", json={
        "task_id": "root-801", "status": "failed", "summary": summary,
        "findings": [], "error_info": {"reason": reason, "final": final,
                                      **({"kind": kind} if kind else {})},
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
        "lineage_end", "root-801", "reason_code=other",
        "test_not_run_provider=true"))
    assert "gemini exit_1" not in notices[0]


def test_no_retry_terminal_failure_notifies_once(tmp_path, monkeypatch):
    app, queue, notices = _harness(tmp_path, monkeypatch, task_type="implement")
    monkeypatch.setenv("AGENT_CREW_RETRY_IMPLEMENT_SELF_FAILED", "0")
    with TestClient(app) as client:
        response = _fail(client, summary="agent stopped", reason="", final=True)
        assert response.status_code == 200, response.text
    assert queue.get_task("retry-root-801-a1") is None
    assert len(notices) == 1
    assert "lineage_end" in notices[0]
    assert "agent stopped" not in notices[0]


def test_notice_does_not_depend_on_coordinator_provenance(tmp_path, monkeypatch):
    app, queue, notices = _harness(tmp_path, monkeypatch, retry_attempt=2)
    queue.patch_context("root-801", {"coordinator_managed": False})
    with TestClient(app) as client:
        response = _fail(client, summary="provider exit_1")
        assert response.status_code == 200, response.text
    assert len(notices) == 1
    assert "lineage_end" in notices[0]


def test_distinct_terminal_tasks_in_one_lineage_each_notify_once(tmp_path, monkeypatch):
    app, queue, notices = _harness(tmp_path, monkeypatch, retry_attempt=2)
    with TestClient(app) as client:
        assert _fail(client, summary="first exit_1").status_code == 200
        queue.enqueue(TaskRequest(
            task_id="retry-root-801-a2", task_type="test", description="provider retry",
            branch="agent/801-failed-no-successor-notice", project="agent_crew",
            context={"coordinator_managed": True, "retry_attempt": 2,
                     "lineage_root_task_id": "root-801"}),
            ingress="http.tasks")
        response = client.post("/tasks/retry-root-801-a2/result", json={
            "task_id": "retry-root-801-a2", "status": "failed",
            "summary": "second exit_1", "findings": [],
            "error_info": {"reason": "exit_1", "final": True},
        })
        assert response.status_code == 200, response.text
        assert len(notices) == 2
        # Replaying the same failed task must reuse its durable notice claim.
        app.state.coordinator_notice("retry-root-801-a2", "lineage_end",
                                     "failed_task=retry-root-801-a2 reason=exit_1")
    assert len(notices) == 2
    assert "task_id=root-801" in notices[0]
    assert "task_id=retry-root-801-a2" in notices[1]
    assert all("exit_1" not in notice for notice in notices)


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
        response = _fail(client, summary="provider 429 rate limit",
                         kind="provider_limit")
        assert response.status_code == 200, response.text
    assert queue.get_task("retry-root-801-a3") is None
    assert any(g.id == "escalation-root-801-provider-limit" for g in queue.list_gates())
    assert notices == []


def test_dispatcher_failed_provider_without_result_notifies_once(
        tmp_path, monkeypatch, unused_tcp_port):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", "0.05")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    worktrees = {}
    for agent in ("codex", "claude", "gemini"):
        worktree = tmp_path / agent
        worktree.mkdir()
        (worktree / ".git").mkdir()
        worktrees[agent] = str(worktree)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "role_agents": {"implementer": "codex", "reviewer": "claude",
                        "tester": "gemini"},
        "worktrees": worktrees,
    }))
    notices = []

    async def failed_process(*_args, **_kwargs):
        process = MagicMock()
        process.returncode = 1
        process.wait = AsyncMock(return_value=1)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failed_process)
    db = str(tmp_path / "tasks.db")
    app = create_app(db, state_path=str(state_file), port=unused_tcp_port,
                     identity_required=False, watchdog_disabled=True,
                     anomaly_disabled=True,
                     coordinator_notify_fn=lambda message: notices.append(message) or True)
    with TestClient(app) as client:
        response = client.post("/tasks", json={
            "task_id": "test-dispatch-801", "task_type": "test",
            "description": "provider test", "branch": "main", "priority": 3,
            "project": "agent_crew", "context": {"coordinator_managed": True},
        })
        assert response.status_code == 201, response.text
        queue = TaskQueue(db)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not notices:
            time.sleep(0.02)
    assert queue.get_task_status("test-dispatch-801") == "failed"
    assert len(notices) == 1
    assert "lineage_end" in notices[0]
    assert "test_not_run_provider=true" in notices[0]
