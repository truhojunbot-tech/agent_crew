"""#309 K1 — durable project coordinator handoff contract."""
import json

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import GateRequest, TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _task(task_id="impl-309", context=None):
    return TaskRequest(
        task_id=task_id, task_type="implement", description="Implement #309",
        branch="feat/309", priority=1, project="portable-project",
        context=context or {"context_id": "worker-context", "pr_number": 309},
    )


def test_coordinator_handoff_is_durable_and_does_not_change_task_lineage(tmp_path):
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    queue.enqueue(_task())
    before = queue.list_tasks()[0]

    first = queue.advance_coordinator(
        coordinator_id="coord-stable", generation=1, provider="provider-a",
        model="model-a", provider_session_id="session-a", handoff_reason="initial",
    )
    changed = queue.advance_coordinator(
        coordinator_id="coord-stable", generation=2, provider="provider-b",
        model="model-b", provider_session_id="session-b", handoff_reason="provider rollover",
        previous_receipt_hash=first["receipt_hash"],
    )

    assert first["accepted"] is True
    assert changed["accepted"] is True
    assert changed["coordinator_generation"] == 2
    assert changed["checkpoint_ref"] == "coordinator:portable-project:coord-stable:2"
    restarted = TaskQueue(str(db))
    after = restarted.list_tasks()[0]
    assert (after.task_id, after.context, after.branch) == (before.task_id, before.context, before.branch)
    assert restarted.get_coordinator_state()["provider_session_id"] == "session-b"


def test_stale_coordinator_generation_is_rejected_and_quarantined(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.advance_coordinator(coordinator_id="coord", generation=4, provider="unknown")

    stale = queue.advance_coordinator(coordinator_id="older", generation=4, provider="other")

    assert stale["accepted"] is False
    assert stale["quarantined"] is True
    assert queue.get_coordinator_state()["coordinator_id"] == "coord"
    assert queue.list_coordinator_receipts(limit=1)[0]["event"] == "stale_generation_rejected"


def test_runtime_export_has_successor_inputs_and_separates_coordinator_from_worker(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.advance_coordinator(
        coordinator_id="coord", generation=1, provider="coordinator-provider",
        model="coordinator-model", provider_session_id="coord-session",
    )
    queue.enqueue(_task())
    queue.create_gate(GateRequest(id="gate-309", type="approval", message="hold", created_at=1.0))
    queue.record_attribution(
        task_id="impl-309", project="portable-project", agent="worker-provider",
        role="implementer", task_type="implement", worktree_path="/worktree",
        git_branch="feat/309", model="worker-model", provider_session_id="worker-session",
        context_id="worker-context",
    )

    exported = queue.export_project_runtime_state()
    assert exported["coordinator"]["coordinator_id"] == "coord"
    assert exported["tasks"][0]["task_id"] == "impl-309"
    assert exported["tasks"][0]["context"]["context_id"] == "worker-context"
    assert exported["gates"][0]["id"] == "gate-309"
    receipt = exported["worker_receipts"][0]
    assert receipt["worker_provider"] == "worker-provider"
    assert receipt["worker_provider_session_id"] == "worker-session"
    assert receipt["coordinator_provider"] == "coordinator-provider"
    assert receipt["coordinator_provider_session_id"] == "coord-session"


def test_unknown_coordinator_is_explicit_and_existing_attribution_remains_opt_in(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.record_attribution(task_id="legacy", project="portable-project", agent="worker")

    state = queue.get_coordinator_state()
    receipt = queue.export_project_runtime_state()["worker_receipts"][0]
    assert state["coordinator_id"] == "unknown"
    assert state["provider"] == "unknown"
    assert receipt["coordinator_id"] == "unknown"
    assert receipt["worker_provider"] == "worker"


def test_generic_runtime_api_rejects_stale_handoff_and_exports_state(tmp_path):
    app = create_app(str(tmp_path / "tasks.db"), pane_map={}, watchdog_disabled=True,
                     anomaly_disabled=True, project="portable-project")
    with TestClient(app) as client:
        accepted = client.post("/runtime/coordinator/handoff", json={
            "coordinator_id": "coord", "generation": 1, "provider": "generic-provider",
        })
        stale = client.post("/runtime/coordinator/handoff", json={
            "coordinator_id": "old", "generation": 1,
        })
        exported = client.get("/runtime/export")
    assert accepted.status_code == 200
    assert stale.status_code == 409
    assert exported.status_code == 200
    assert exported.json()["coordinator"]["coordinator_id"] == "coord"
