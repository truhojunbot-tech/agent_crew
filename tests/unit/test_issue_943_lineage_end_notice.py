"""#943: a terminal pipeline leaf wakes the coordinator exactly once."""

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    db = str(tmp_path / "tasks.db")
    notices = []
    app = create_app(db, project="agent_crew", pane_map={}, identity_required=False,
                     watchdog_disabled=True, anomaly_disabled=True,
                     coordinator_notify_fn=lambda message: notices.append(message) or True)
    return app, TaskQueue(db), notices


def _task(queue, task_id, task_type="discuss", **context):
    queue.enqueue(TaskRequest(task_id=task_id, task_type=task_type,
                              description="task", branch="agent/943", project="agent_crew",
                              context={"risk_tier": 2, "issue": 943, **context}),
                  ingress="http.tasks")


def _result(client, task_id, status="completed", **fields):
    return client.post(f"/tasks/{task_id}/result", json={
        "task_id": task_id, "status": status, "summary": "x" * 350,
        "findings": [], **fields})


def test_A_standalone_completion_has_one_structured_end_notice(harness):
    app, queue, notices = harness
    _task(queue, "research-943")
    with TestClient(app) as client:
        response = _result(client, "research-943")
    assert response.status_code == 200, response.text
    assert len(notices) == 1
    assert all(s in notices[0] for s in ("kind=lineage_end", "task_id=research-943",
                                         "task_type=discuss", "status=completed",
                                         "lineage_head=research-943", "issue=943", "summary="))
    assert "x" * 300 in notices[0]
    assert "x" * 301 not in notices[0]


def test_B_successor_suppresses_parent_and_merged_leaf_carries_pr(harness, monkeypatch):
    app, queue, notices = harness
    _task(queue, "impl-943", "implement")
    _task(queue, "review-943", "review", prev_task_id="impl-943", pr_number=943)
    _task(queue, "test-943", "test", prev_task_id="review-943", pr_number=943,
          repo="owner/repo")
    monkeypatch.setattr("agent_crew.github.pr_head_sha", lambda *a, **k: "a" * 40)
    monkeypatch.setattr("agent_crew.github.pr_state", lambda *a, **k: "merged")
    with TestClient(app) as client:
        assert _result(client, "impl-943", pr_number=943).status_code == 200
        assert _result(client, "review-943", verdict="approve", pr_number=943).status_code == 200
        assert _result(client, "test-943", pr_number=943).status_code == 200
    assert len(notices) == 1
    assert queue.external_op_get("merge:pr:943")["state"] == "done"
    assert "task_id=test-943" in notices[0]
    assert "pr_number=943" in notices[0]
    assert "lineage_head=impl-943" in notices[0]


@pytest.mark.parametrize("status", ["needs_human", "failed"])
def test_C_terminal_without_retry_notifies(harness, monkeypatch, status):
    app, queue, notices = harness
    _task(queue, f"leaf-{status}")
    monkeypatch.setenv("AGENT_CREW_RETRY_IMPLEMENT_SELF_FAILED", "0")
    with TestClient(app) as client:
        assert _result(client, f"leaf-{status}", status).status_code == 200
    assert len(notices) == 1
    assert f"status={status}" in notices[0]


def test_C_cancelled_notifies(harness):
    app, queue, notices = harness
    _task(queue, "cancel-943")
    with TestClient(app) as client:
        assert client.delete("/tasks/cancel-943").status_code == 200
    assert len(notices) == 1
    assert "status=cancelled" in notices[0]


def test_D_retry_suppresses_end_until_exhaustion(harness):
    app, queue, notices = harness
    _task(queue, "impl-retry-943", "implement")
    with TestClient(app) as client:
        assert _result(client, "impl-retry-943", "failed").status_code == 200
    assert queue.get_task("retry-impl-retry-943-a1") is not None
    assert notices == []
    _task(queue, "exhausted-943", "implement", retry_attempt=2)
    with TestClient(app) as client:
        assert _result(client, "exhausted-943", "failed").status_code == 200
    assert queue.get_task("retry-exhausted-943-a3") is None
    assert len(notices) == 1 and "task_id=exhausted-943" in notices[0]


def test_E_duplicate_result_does_not_duplicate_notice(harness):
    app, queue, notices = harness
    _task(queue, "once-943")
    with TestClient(app) as client:
        assert _result(client, "once-943").status_code == 200
        assert _result(client, "once-943").status_code in (200, 409)
    assert len(notices) == 1


def test_F_exhausted_provider_failure_uses_lineage_end_once(harness):
    app, queue, notices = harness
    _task(queue, "test-failed-943", "test", retry_attempt=2)
    with TestClient(app) as client:
        assert _result(client, "test-failed-943", "failed",
                       error_info={"reason": "exit_1", "final": True}).status_code == 200
    assert len(notices) == 1
    assert "kind=lineage_end" in notices[0]
    assert "test not run (provider)" in notices[0]
