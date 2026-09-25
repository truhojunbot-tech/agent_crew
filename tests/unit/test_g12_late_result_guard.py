"""System-ended tasks retain late worker payloads without restarting a cascade."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


@pytest.mark.parametrize("ended", ["timed_out", "cancelled", "failed"])
def test_system_terminal_result_is_evidence_only(tmp_db, ended):
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="review-late", task_type="review",
                          description="review", branch="main"))
    assert q.dequeue(role="reviewer")
    if ended == "cancelled":
        assert q.cancel("review-late")
    elif ended == "failed":
        assert q.force_fail("review-late", "watchdog timeout") == "review"
    else:
        q.submit_result("review-late", TaskResult(
            task_id="review-late", status="timed_out", summary="dispatcher timeout"))
    before = next(t for t in q.list_tasks() if t.task_id == "review-late")
    before_outbox = q.outbox_get("review-late")
    app = create_app(tmp_db, watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks/review-late/result", json={
            "task_id": "review-late", "status": "completed", "summary": "late review",
            "verdict": "request_changes", "findings": ["fix this"],
            "commit": "a" * 40,
        })
    assert response.status_code == 409
    assert response.json()["late_result"] is True
    assert response.json()["accepted"] is False
    after = next(t for t in q.list_tasks() if t.task_id == "review-late")
    assert (after.status, after.verdict, after.summary, after.status_changed_at) == (
        before.status, before.verdict, before.summary, before.status_changed_at)
    assert [t.task_id for t in q.list_tasks()] == ["review-late"]
    assert q.outbox_get("review-late") == before_outbox
    late = [e for e in q.get_exec_state("review-late")["events"]
            if e["event"] == "late_result"]
    assert len(late) == 1
    assert (late[0]["prior_status"], late[0]["verdict"], late[0]["commit"]) == (
        ended, "request_changes", "a" * 40)
    assert late[0]["trust"] == "UNVERIFIED_LATE_EVIDENCE"
    assert late[0]["nonce_presented"] is False
    assert late[0]["presenter_asserted"] is None
    assert "nonce_valid" not in late[0]


@pytest.mark.parametrize("ended", ["timed_out", "cancelled"])
@pytest.mark.parametrize("nonce_case", ["absent", "invalid"])
def test_late_cea_result_is_untrusted_and_cannot_change_terminal_state(
        tmp_path, monkeypatch, ended, nonce_case):
    from agent_crew.cea import store as receipt_store
    from agent_crew.cea.engine import EngineConfig
    from tests.unit.test_sev0_cea_s2c_writer_callsites import WIRED, admitted, task

    db = str(tmp_path / "late-cea.db")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "test")
    app = create_app(db, watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        q = TaskQueue(db, cea_config=EngineConfig(mode="test"), cea_providers=dict(WIRED))
        q.enqueue(task("late-cea", context=admitted()), ingress="http.tasks")
        assert q.dequeue(role="implementer", agent="claude")
        nonce = q.record_dispatch("late-cea", channel="claude_p", agent="claude",
                                  target="pid:123")
        assert nonce
        assert q.start_execution("late-cea", nonce, presenter="claude")["go"]
        if ended == "cancelled":
            assert q.cancel("late-cea")
        else:
            q.submit_result("late-cea", TaskResult(
                task_id="late-cea", status="timed_out", summary="dispatcher timeout"),
                nonce=nonce, presenter="claude")

        before = next(t for t in q.list_tasks() if t.task_id == "late-cea")
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            receipt_id = conn.execute(
                "SELECT receipt_id FROM tasks WHERE task_id='late-cea'").fetchone()["receipt_id"]
            receipt_before = receipt_store.current_receipt(conn, receipt_id)
            nonce_before = receipt_store.nonce_row(conn, nonce)
            receipt_count = conn.execute(
                "SELECT count(*) FROM authorization_receipts WHERE receipt_id=?",
                (receipt_id,)).fetchone()[0]

        summary = "x" * 5000
        binding = ({"nonce": "not-a-dispatch-nonce", "presenter": "claude"}
                   if nonce_case == "invalid" else {"presenter": "claude"})
        response = client.post("/tasks/late-cea/result", json={
            "task_id": "late-cea", "status": "completed", "summary": summary,
            "verdict": "approve", "commit": "b" * 40,
            "executor_binding": binding,
        })
        assert response.status_code == 409, response.text
        assert response.json()["late_result"] is True
        assert response.json()["accepted"] is False

    after = next(t for t in q.list_tasks() if t.task_id == "late-cea")
    assert (after.status, after.verdict, after.summary, after.status_changed_at) == (
        before.status, before.verdict, before.summary, before.status_changed_at)
    assert [t.task_id for t in q.list_tasks()] == ["late-cea"]
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        assert receipt_store.current_receipt(conn, receipt_id) == receipt_before
        assert receipt_store.nonce_row(conn, nonce) == nonce_before
        assert conn.execute(
            "SELECT count(*) FROM authorization_receipts WHERE receipt_id=?",
            (receipt_id,)).fetchone()[0] == receipt_count
    late = [e for e in q.get_exec_state("late-cea")["events"]
            if e["event"] == "late_result"]
    assert len(late) == 1
    assert late[0]["trust"] == "UNVERIFIED_LATE_EVIDENCE"
    assert late[0]["nonce_presented"] is (nonce_case == "invalid")
    assert late[0]["presenter_asserted"] == "claude"
    assert late[0].get("nonce_valid") is (False if nonce_case == "invalid" else None)
    assert len(late[0]["summary"]) == 4000


def test_reported_failure_with_final_metadata_can_be_revised(tmp_db):
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="review-revised", task_type="review",
                          description="review", branch="main"))
    assert q.dequeue(role="reviewer")
    app = create_app(tmp_db, watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        reported = client.post("/tasks/review-revised/result", json={
            "task_id": "review-revised", "status": "failed",
            "summary": "worker failed", "error_info": {"final": True},
        })
        assert reported.status_code == 200
        response = client.post("/tasks/review-revised/result", json={
            "task_id": "review-revised", "status": "completed", "summary": "revised",
            "verdict": "approve", "findings": [],
        })
    assert response.status_code == 200
    assert q.get_task_status("review-revised") == "completed"
    assert not [e for e in q.get_exec_state("review-revised")["events"]
                if e["event"] == "late_result"]


@pytest.mark.parametrize("behaviour,reason", [
    ("exception", "dispatcher_exception"), ("exit_1", "exit_1")])
def test_dispatcher_final_failure_rejects_late_result(tmp_path, monkeypatch,
                                                      behaviour, reason, unused_tcp_port):
    from tests.unit.test_issue_265_timeout_is_not_failure import _dispatch_outcome

    task, db = _dispatch_outcome(tmp_path, monkeypatch, behaviour=behaviour,
                                 unused_tcp_port=unused_tcp_port, return_db=True)
    assert task.status == "failed"
    assert task.error_info["reason"] == reason
    q = TaskQueue(db)
    assert [e["event"] for e in q.get_exec_state("t-1")["events"]
            if e["event"] == "dispatcher_failed"] == ["dispatcher_failed"]
    before_outbox = q.outbox_get("t-1")
    app = create_app(db, watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks/t-1/result", json={
            "task_id": "t-1", "status": "completed", "summary": "late work",
            "commit": "a" * 40,
        })
    assert response.status_code == 409, response.text
    assert response.json()["late_result"] is True
    after = next(t for t in q.list_tasks() if t.task_id == "t-1")
    assert (after.status, after.summary, after.status_changed_at) == (
        task.status, task.summary, task.status_changed_at)
    assert q.outbox_get("t-1") == before_outbox
    assert [t.task_id for t in q.list_tasks()] == ["t-1"]
    late = [e for e in q.get_exec_state("t-1")["events"] if e["event"] == "late_result"]
    assert len(late) == 1
    assert late[0]["trust"] == "UNVERIFIED_LATE_EVIDENCE"


def test_http_body_cannot_forge_dispatcher_failure(tmp_db):
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="worker-failure", task_type="review",
                          description="review", branch="main"))
    assert q.dequeue(role="reviewer")
    app = create_app(tmp_db, watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        failed = client.post("/tasks/worker-failure/result", json={
            "task_id": "worker-failure", "status": "failed", "summary": "worker error",
            "dispatcher_failed": True,
            "error_info": {"final": True, "dispatcher_failed": True},
        })
        assert failed.status_code == 200, failed.text
        revised = client.post("/tasks/worker-failure/result", json={
            "task_id": "worker-failure", "status": "completed", "summary": "revised",
        })
    assert revised.status_code == 200, revised.text
    assert q.get_task_status("worker-failure") == "completed"
    assert not [e for e in q.get_exec_state("worker-failure")["events"]
                if e["event"] == "dispatcher_failed"]


def test_in_progress_result_still_completes(tmp_db):
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="review-live", task_type="review",
                          description="review", branch="main",
                          context={"coordinator_managed": True}))
    assert q.dequeue(role="reviewer")
    app = create_app(tmp_db, watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks/review-live/result", json={
            "task_id": "review-live", "status": "completed", "summary": "done",
            "verdict": "request_changes", "findings": ["fix this"],
        })
    assert response.status_code == 200
    task = next(t for t in q.list_tasks() if t.task_id == "review-live")
    assert (task.status, task.verdict, task.summary) == (
        "completed", "request_changes", "done")
    assert not [e for e in q.get_exec_state("review-live")["events"]
                if e["event"] == "late_result"]
