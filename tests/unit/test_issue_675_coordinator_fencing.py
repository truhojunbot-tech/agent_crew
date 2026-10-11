"""C1.a: coordinator and worker attempts are fenced by durable generations."""

from dataclasses import asdict
import io
import json
import urllib.error
import urllib.request

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
    ready = queue.prepare_coordinator_handoff({
        "objective": "handoff", "open_task_ids": ["old-task"],
        "last_receipt_hash": "prior", "generation": 1})
    assert queue.advance_coordinator(coordinator_id="new", generation=2,
                                     checkpoint_ref=ready["checkpoint_ref"])["accepted"]

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


def test_queue_rotates_real_dispatch_attempt_and_accepts_current_result(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(_task("rotated"))
    queue.record_dispatch("rotated", channel="api")
    first = queue.dispatch_binding("rotated")["attempt_id"]
    queue.record_dispatch("rotated", channel="api")
    second = queue.dispatch_binding("rotated")["attempt_id"]
    assert first and second and first != second

    with pytest.raises(StaleAttemptRejected, match="stale_attempt"):
        queue.submit_result("rotated", TaskResult(
            task_id="rotated", status="completed", summary="old result"),
            attempt_id=first)
    assert queue.get_task("rotated").status != "completed"
    queue.submit_result("rotated", TaskResult(
        task_id="rotated", status="completed", summary="current result"),
        attempt_id=second)
    assert queue.get_task("rotated").status == "completed"
    assert queue.stale_attempt_count() == 1


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
    with pytest.raises(ValueError, match="prepared checkpoint_ref"):
        queue.advance_coordinator(coordinator_id="new", generation=2,
                                  checkpoint_ref="arbitrary")
    assert queue.overdue_coordinator_handoff(after_seconds=0)["event"] == "handoff_checkpoint_ready"
    queue.advance_coordinator(coordinator_id="new", generation=2,
                              checkpoint_ref=ready["checkpoint_ref"])
    assert queue.overdue_coordinator_handoff(after_seconds=0) is None


def test_http_rejects_stale_coordinator_writes_and_worker_result(tmp_path):
    db = str(tmp_path / "tasks.db")
    app = create_app(db, pane_map={}, watchdog_disabled=True,
                     anomaly_disabled=True, project="demo")
    with TestClient(app, headers={"X-Agent-Crew-Project": "demo"}) as client:
        assert client.post("/runtime/coordinator/handoff", json={
            "coordinator_id": "old", "generation": 1}).status_code == 200
        task = asdict(_task("http-task"))
        assert client.post("/tasks", json=task,
                           headers={"X-Agent-Crew-Coordinator-Generation": "1"}).status_code == 201
        ready = client.post("/runtime/coordinator/checkpoint", json={
            "objective": "handoff", "open_task_ids": ["http-task"],
            "last_receipt_hash": "prior", "generation": 1}).json()
        assert client.post("/runtime/coordinator/handoff", json={
            "coordinator_id": "new", "generation": 2,
            "checkpoint_ref": ready["checkpoint_ref"]}).status_code == 200
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


def test_http_rotates_real_dispatch_attempt_and_accepts_current_result(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    db = str(tmp_path / "tasks.db")
    app = create_app(db, pane_map={}, watchdog_disabled=True,
                     anomaly_disabled=True, project="demo", identity_required=False)
    with TestClient(app, headers={"X-Agent-Crew-Project": "demo"}) as client:
        assert client.post("/tasks", json=asdict(_task("http-rotated"))).status_code == 201
        queue = TaskQueue(db)
        queue.record_dispatch("http-rotated", channel="api")
        first = queue.dispatch_binding("http-rotated")["attempt_id"]
        queue.record_dispatch("http-rotated", channel="api")
        second = queue.dispatch_binding("http-rotated")["attempt_id"]
        assert first and second and first != second

        stale = client.post("/tasks/http-rotated/result", json={
            "task_id": "http-rotated", "status": "completed", "summary": "old result",
            "attempt_id": first})
        assert stale.status_code == 409
        assert "stale_attempt" in stale.text
        assert queue.get_task("http-rotated").status != "completed"
        accepted = client.post("/tasks/http-rotated/result", json={
            "task_id": "http-rotated", "status": "completed", "summary": "current result",
            "attempt_id": second})
        assert accepted.status_code == 200
        assert queue.get_task("http-rotated").status == "completed"


def test_crew_run_http_enqueue_sends_generation_and_refuses_old_claim(tmp_path, monkeypatch):
    from agent_crew.loop import enqueue_implement

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    db = str(tmp_path / "tasks.db")
    app = create_app(db, pane_map={}, watchdog_disabled=True,
                     anomaly_disabled=True, project="demo", identity_required=False)
    sent_headers = []
    sent_task_ids = []

    with TestClient(app) as client:
        def urlopen(request, timeout):
            headers = dict(request.header_items())
            sent_headers.append(headers)
            sent_task_ids.append(json.loads(request.data)["task_id"])
            response = client.request(request.get_method(), request.full_url,
                                      content=request.data, headers=headers)
            if response.status_code >= 400:
                raise urllib.error.HTTPError(request.full_url, response.status_code,
                                             response.text, None, io.BytesIO(response.content))
            return io.BytesIO(response.content)

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        assert client.post("/runtime/coordinator/handoff", json={
            "coordinator_id": "crew-run:demo", "generation": 1}).status_code == 200
        queue = TaskQueue(db)
        current_id = enqueue_implement(
            queue, "current run", "main",
            context={"coordinator_generation": 1, "risk_tier": 2},
            port=8105, project="demo")
        assert queue.get_task(current_id) is not None
        assert sent_headers[-1]["X-agent-crew-coordinator-generation"] == "1"
        assert queue.cancel(current_id, coordinator_generation=1)

        ready = queue.prepare_coordinator_handoff({
            "objective": "new run", "open_task_ids": [current_id],
            "last_receipt_hash": "prior", "generation": 1})
        assert queue.advance_coordinator(coordinator_id="new", generation=2,
                                         checkpoint_ref=ready["checkpoint_ref"])["accepted"]
        with pytest.raises(urllib.error.HTTPError) as error:
            enqueue_implement(queue, "old run", "main",
                              context={"coordinator_generation": 1, "risk_tier": 2},
                              port=8105, project="demo")
        assert error.value.code == 409
        assert "stale_coordinator_generation" in error.value.read().decode()
        assert sent_headers[-1]["X-agent-crew-coordinator-generation"] == "1"
        assert queue.get_task(sent_task_ids[-1]) is None


def test_headerless_worker_enqueue_and_cancel_survive_first_handoff(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    app = create_app(str(tmp_path / "tasks.db"), pane_map={}, project="demo",
                     watchdog_disabled=True, anomaly_disabled=True,
                     identity_required=False)
    with TestClient(app) as client:
        assert client.post("/runtime/coordinator/handoff", json={
            "coordinator_id": "first", "generation": 1}).status_code == 200
        assert client.post("/tasks", json=asdict(_task("worker-task"))).status_code == 201
        assert client.delete("/tasks/worker-task").status_code == 200


def test_http_allows_legacy_result_without_attempt_but_fences_stale_probe(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    db = str(tmp_path / "tasks.db")
    app = create_app(db, pane_map={}, project="demo", watchdog_disabled=True,
                     anomaly_disabled=True, identity_required=False)
    with TestClient(app) as client:
        assert client.post("/tasks", json=asdict(_task("legacy"))).status_code == 201
        assert client.post("/tasks", json=asdict(_task("bound"))).status_code == 201
        queue = TaskQueue(db)
        queue.record_dispatch("legacy", channel="api")
        queue.record_dispatch("bound", channel="api")
        legacy = client.post("/tasks/legacy/result", json={
            "task_id": "legacy", "status": "completed", "summary": "done"})
        assert legacy.status_code == 200
        assert queue.get_task("legacy").status == "completed"
        assert queue.stale_attempt_count() == 0
        stale = client.post("/tasks/bound/result", json={
            "task_id": "bound", "status": "completed", "summary": "done",
            "attempt_id": "old"})
        assert stale.status_code == 409
        assert queue.get_task("bound").status != "completed"
        assert queue.stale_attempt_count() == 1


def test_mcp_allows_legacy_result_without_attempt_but_fences_stale_probe(tmp_path):
    from agent_crew.mcp_server import build_mcp_server

    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    queue.enqueue(_task("legacy-mcp"))
    queue.enqueue(_task("bound-mcp"))
    queue.record_dispatch("legacy-mcp", channel="api")
    queue.record_dispatch("bound-mcp", channel="api")
    submit = build_mcp_server(db)._tool_manager._tools["submit_result"].fn
    accepted = submit(task_id="legacy-mcp", status="completed", summary="done")
    assert accepted["acknowledged"] is True
    assert queue.get_task("legacy-mcp").status == "completed"
    rejected = submit(task_id="bound-mcp", status="completed", summary="done",
                      attempt_id="old")
    assert rejected["stale_attempt"] is True
    assert queue.get_task("bound-mcp").status != "completed"


def test_push_message_and_protocol_include_attempt(tmp_path, monkeypatch):
    from agent_crew.instructions import generate

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setenv("AGENT_CREW_DELIVERY", "push")
    pushed = []
    app = create_app(str(tmp_path / "tasks.db"), project="demo", port=8105,
                     pane_map={"implementer": "%101"},
                     push_fn=lambda pane, message: pushed.append(message),
                     pane_busy_fn=lambda pane: False, watchdog_disabled=True,
                     anomaly_disabled=True, identity_required=False)
    with TestClient(app) as client:
        payload = asdict(_task("pushed"))
        payload["task_type"] = "implement"
        assert client.post("/tasks", json=payload).status_code == 201
        binding = TaskQueue(str(tmp_path / "tasks.db")).dispatch_binding("pushed")
    assert len(pushed) == 1
    assert f'attempt_id: {binding["attempt_id"]}' in pushed[0]
    assert f'"attempt_id":"{binding["attempt_id"]}"' in pushed[0]
    protocol = generate("implementer", "demo", 8105, delivery="dispatcher")
    assert "attempt_id" in protocol
    assert "omitted attempts remain accepted" in protocol.lower()


def test_overlapping_crew_runs_join_one_coordinator_generation(tmp_path):
    from agent_crew.cli import _start_or_renew_run_coordinator

    queue = TaskQueue(str(tmp_path / "tasks.db"))
    first = _start_or_renew_run_coordinator(queue, "demo")
    second = _start_or_renew_run_coordinator(queue, "demo")
    assert first == second == 1
    queue.enqueue(_task("first-run-task"), coordinator_generation=first)
    queue.enqueue(_task("second-run-task"), coordinator_generation=second)
    assert queue.get_coordinator_state()["coordinator_id"] == "crew-run:demo"
    assert [r["event"] for r in queue.list_coordinator_receipts()].count(
        "handoff_accepted") == 1
    assert not any(r["event"] == "handoff_checkpoint_ready"
                   for r in queue.list_coordinator_receipts())
    queue.prepare_coordinator_handoff({
        "objective": "owner handoff", "open_task_ids": ["first-run-task", "second-run-task"],
        "last_receipt_hash": queue.list_coordinator_receipts()[0]["receipt_hash"], "generation": 1})
    ready = next(r for r in queue.list_coordinator_receipts()
                 if r["event"] == "handoff_checkpoint_ready")
    queue.advance_coordinator(coordinator_id="other", generation=2,
                              checkpoint_ref=ready["receipt_hash"])
    with pytest.raises(Exception, match="explicit handoff"):
        _start_or_renew_run_coordinator(queue, "demo")


def test_simultaneous_first_crew_run_claim_joins_winner(tmp_path, monkeypatch):
    from agent_crew.cli import _start_or_renew_run_coordinator

    queue = TaskQueue(str(tmp_path / "tasks.db"))
    advance = queue.advance_coordinator

    def raced_advance(**kwargs):
        assert advance(**kwargs)["accepted"]
        return advance(**kwargs)

    monkeypatch.setattr(queue, "advance_coordinator", raced_advance)
    assert _start_or_renew_run_coordinator(queue, "demo") == 1
    assert queue.get_coordinator_state()["coordinator_generation"] == 1
    assert [r["event"] for r in queue.list_coordinator_receipts()].count(
        "handoff_accepted") == 1


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
