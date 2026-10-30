"""#943: a terminal pipeline leaf wakes the coordinator exactly once."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent_crew.mcp_server import build_mcp_server
from agent_crew.pipeline import fix_task_id
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
                                         "lineage_head=research-943", "issue=943"))
    assert "summary=" not in notices[0]
    assert "x" * 300 not in notices[0]


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
    assert "test_not_run_provider=true" in notices[0]


def test_C3_unknown_reason_and_summary_do_not_reach_notice_sink(harness, monkeypatch):
    app, queue, notices = harness
    secret = "SENTINEL_SECRET_943C3"
    _task(queue, "leaf-secret-943")
    monkeypatch.setenv("AGENT_CREW_RETRY_IMPLEMENT_SELF_FAILED", "0")
    with TestClient(app) as client:
        response = _result(client, "leaf-secret-943", "failed",
                           summary=f"provider wrote {secret}",
                           error_info={"reason": f"unexpected {secret}", "final": True})
    assert response.status_code == 200, response.text
    assert len(notices) == 1
    delivered = notices[0]  # harness captures the actual notify sink argument
    assert secret not in delivered
    assert "reason_code=other" in delivered
    assert "summary=" not in delivered
    fields = dict(part.split("=", 1) for part in delivered.split()
                  if "=" in part)
    assert set(fields) == {"kind", "task_id", "lineage_head", "task_type",
                           "status", "pr_number", "issue", "reason_code",
                           "test_not_run_provider"}


def test_C3_known_reason_code_passes_through(harness):
    app, queue, notices = harness
    _task(queue, "known-reason-943")
    with TestClient(app) as client:
        response = _result(client, "known-reason-943", "failed",
                           error_info={"reason": "no_artifact", "final": True})
    assert response.status_code == 200, response.text
    assert len(notices) == 1
    assert "reason_code=no_artifact" in notices[0]


def test_C3_formatter_rejects_invalid_identity_and_untyped_values():
    from agent_crew.server import _lineage_end_detail
    detail = _lineage_end_detail(
        task_id="unsafe id\nSENTINEL_SECRET_943C3", lineage_head="root id with spaces",
        task_type="not-a-task-type", status="made_up", pr_number="not a PR",
        issue="private path /tmp/secret", reason="secret reason",
        provider_not_run=False)
    assert "SENTINEL_SECRET_943C3" not in detail
    assert "secret reason" not in detail
    assert "task_id=invalid" in detail and "lineage_head=invalid" in detail
    assert "task_type=invalid" in detail and "status=invalid" in detail
    assert "pr_number= issue=" in detail
    assert "reason_code=other" in detail


def test_refused_review_fix_has_only_the_existing_successor_refused_notice(
        harness, monkeypatch):
    app, queue, notices = harness
    monkeypatch.setattr("agent_crew.github.pr_state", lambda *a, **k: "open")
    monkeypatch.setattr("agent_crew.github.post_review_comment", lambda *a, **k: False)
    queue.enqueue(TaskRequest(
        task_id="review-refused-943", task_type="review", description="review",
        branch="main", project="agent_crew",
        context={"risk_tier": 2, "issue": 943, "pr_number": 943,
                 "repo": "owner/repo"}), ingress="http.tasks")
    with TestClient(app) as client:
        response = _result(client, "review-refused-943", verdict="request_changes",
                           findings=["HIGH src/x.py:1 - fix required"], pr_number=943)
    assert response.status_code == 200, response.text
    assert queue.get_task("review-refused-943").context["lineage_terminal_reason"] == (
        "successor_refused: default_branch")
    assert len(notices) == 1
    assert "kind=successor_refused" in notices[0]


def test_review_fix_created_by_server_suppresses_end_notice(harness, monkeypatch):
    app, queue, notices = harness
    monkeypatch.setattr("agent_crew.github.pr_state", lambda *a, **k: "open")
    monkeypatch.setattr("agent_crew.github.post_review_comment", lambda *a, **k: False)
    _task(queue, "review-fix-943", "review", pr_number=943,
          repo="owner/repo", base_branch="main")
    with TestClient(app) as client:
        response = _result(client, "review-fix-943", verdict="request_changes",
                           findings=["HIGH src/x.py:1 - fix required"], pr_number=943)
    assert response.status_code == 200, response.text
    assert queue.get_task(fix_task_id("review-fix-943", 1)) is not None
    assert notices == []


@pytest.mark.parametrize("pr_number", [None, 943])
def test_test_pass_without_merge_successor_notifies(harness, monkeypatch, pr_number):
    app, queue, notices = harness
    monkeypatch.setenv("AGENT_CREW_AUTO_MERGE", "0")
    ctx = {"pr_number": pr_number} if pr_number else {}
    _task(queue, "test-pass-943", "test", **ctx)
    with TestClient(app) as client:
        response = _result(client, "test-pass-943", pr_number=pr_number)
    assert response.status_code == 200, response.text
    assert len(notices) == 1
    assert "kind=lineage_end" in notices[0]


@pytest.mark.parametrize("status", ["failed", "needs_human"])
def test_dispatcher_ended_task_uses_lineage_end(harness, status):
    app, queue, notices = harness
    _task(queue, f"dispatcher-{status}")
    with sqlite3.connect(queue.db_path) as db:
        db.execute("UPDATE tasks SET status='in_progress' WHERE task_id=?",
                   (f"dispatcher-{status}",))
    with TestClient(app):
        assert app.state.fail_if_active(f"dispatcher-{status}", "provider stopped",
                                        status=status)
    assert queue.get_task_status(f"dispatcher-{status}") == status
    assert len(notices) == 1
    assert "kind=lineage_end" in notices[0]


def test_mcp_completed_leaf_notifies_and_fallback_continuation_does_not(
        harness, monkeypatch):
    _, queue, _ = harness
    _task(queue, "mcp-leaf-943")
    _task(queue, "mcp-continuing-943")
    sent = []
    monkeypatch.setattr("agent_crew.server._deliver_coordinator_notice",
                        lambda message: sent.append(message) or True)
    mcp = build_mcp_server(queue.db_path)
    submit = mcp._tool_manager._tools["submit_result"].fn
    assert submit(task_id="mcp-leaf-943", status="completed",
                  summary="research complete")["acknowledged"]
    assert len(sent) == 1 and "kind=lineage_end" in sent[0]

    monkeypatch.setattr("agent_crew.mcp_server.auto_fallback_failed_task",
                        lambda *a, **k: True)
    assert submit(task_id="mcp-continuing-943", status="failed",
                  summary="provider limited")["acknowledged"]
    assert len(sent) == 1
