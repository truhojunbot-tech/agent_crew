"""C1.a: coordinator and worker attempts are fenced by durable generations."""

from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import (StaleAttemptRejected, StaleCoordinatorGeneration,
                              TaskQueue)
from agent_crew.server import create_app


def _task(task_id):
    return TaskRequest(task_id=task_id, task_type="discuss", description="C1.a probe",
                       branch="main", project="demo", context={"risk_tier": 2})


def test_split_brain_generations_and_old_worker_attempt_are_fenced(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    assert queue.advance_coordinator(coordinator_id="old", generation=1)["accepted"]
    queue.enqueue(_task("old-task"), coordinator_generation=1)
    assert queue.advance_coordinator(coordinator_id="new", generation=2)["accepted"]

    with pytest.raises(StaleCoordinatorGeneration):
        queue.enqueue(_task("stale-task"), coordinator_generation=1)
    with pytest.raises(StaleCoordinatorGeneration):
        queue.cancel("old-task", coordinator_generation=1)
    queue.enqueue(_task("new-task"), coordinator_generation=2)
    assert queue.cancel("old-task", coordinator_generation=2)
    assert queue.get_task("stale-task") is None

    queue.record_dispatch("new-task", channel="api")
    binding = queue.dispatch_binding("new-task")
    assert binding["coordinator_generation"] == 2
    assert binding["attempt_id"]
    with pytest.raises(StaleAttemptRejected):
        queue.submit_result("new-task", TaskResult(task_id="new-task", status="completed",
                                                   summary="done"),
                            attempt_id="old-attempt")
    assert queue.get_task("new-task").status != "completed"
    assert queue.stale_attempt_count() == 1
    assert [r["event"] for r in queue.list_coordinator_receipts()].count(
        "stale_coordinator_generation") == 2


def test_handoff_requires_complete_checkpoint(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.advance_coordinator(coordinator_id="old", generation=1)
    with pytest.raises(ValueError, match="objective.*open_task_ids.*last_receipt_hash.*generation"):
        queue.advance_coordinator(coordinator_id="new", generation=2,
                                  checkpoint={})
    assert queue.get_coordinator_state()["coordinator_generation"] == 1
    ready = queue.prepare_coordinator_handoff({
        "objective": "finish current work", "open_task_ids": [],
        "last_receipt_hash": "receipt-1", "generation": 1})
    assert ready["checkpoint_ref"]
    assert queue.overdue_coordinator_handoff(after_seconds=0)["event"] == "handoff_checkpoint_ready"
    queue.advance_coordinator(coordinator_id="new", generation=2,
                              checkpoint_ref=ready["checkpoint_ref"])
    assert queue.overdue_coordinator_handoff(after_seconds=0) is None


def test_http_rejects_stale_coordinator_writes_and_worker_result(tmp_path):
    db = str(tmp_path / "tasks.db")
    app = create_app(db, pane_map={}, watchdog_disabled=True,
                     anomaly_disabled=True, project="demo")
    with TestClient(app) as client:
        assert client.post("/runtime/coordinator/handoff", json={
            "coordinator_id": "old", "generation": 1}).status_code == 200
        task = asdict(_task("http-task"))
        assert client.post("/tasks", json=task,
                           headers={"X-Agent-Crew-Coordinator-Generation": "1"}).status_code == 201
        assert client.post("/runtime/coordinator/handoff", json={
            "coordinator_id": "new", "generation": 2}).status_code == 200
        assert client.post("/tasks", json=asdict(_task("stale-http")),
                           headers={"X-Agent-Crew-Coordinator-Generation": "1"}).status_code == 409
        assert client.delete("/tasks/http-task", headers={
            "X-Agent-Crew-Coordinator-Generation": "1"}).status_code == 409
        queue = TaskQueue(db)
        queue.record_dispatch("http-task", channel="api")
        result = {"task_id": "http-task", "status": "completed", "summary": "done",
                  "attempt_id": "obsolete"}
        refused = client.post("/tasks/http-task/result", json=result)
        assert refused.status_code == 409
        assert "stale_attempt" in refused.text
        assert queue.stale_attempt_count() == 1


def test_resume_cea_env_overrides_shell_and_names_missing_pieces(tmp_path, monkeypatch):
    from agent_crew.cli import _load_resume_cea_env, _resume_missing_pieces

    project_dir = tmp_path / "demo"
    project_dir.mkdir()
    (project_dir / "cea.env").write_text("AGENT_CREW_CEA_SNAPSHOT_PATH=/verified/snapshot.json\n")
    monkeypatch.setenv("AGENT_CREW_CEA_SNAPSHOT_PATH", "/wrong/snapshot.json")
    assert _load_resume_cea_env(str(project_dir))["AGENT_CREW_CEA_SNAPSHOT_PATH"] == "/verified/snapshot.json"
    assert _resume_missing_pieces(
        verifier_env=False, decision_id="t0", snapshot_generation=7,
        decision_present=False, principals=(), build_bound=False,
        generation=2, current_generation=2, cea_path=str(project_dir / "cea.env")) == [
            f"no verifier env ({project_dir / 'cea.env'} absent)",
            "decision t0 not in snapshot gen 7", "principals empty",
            "build not bound", "stale resume generation 2 <= current 2"]
