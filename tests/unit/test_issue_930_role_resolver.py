"""#930 A: malformed role configuration must not select another provider."""

import ast
import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_crew.role_mapping import RoleResolutionError, effective_role_mapping, resolve_agent_role
from agent_crew.server import _load_role_to_agent_with_source, create_app
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


VALID = {"implementer": "codex", "reviewer": "claude", "tester": "script"}


def test_invalid_explicit_mapping_refuses_without_legacy_fallback():
    with pytest.raises(RoleResolutionError, match="invalid_role_agents"):
        effective_role_mapping({"role_agents": {"implementer": "codex"},
                                "roles": [{"role": "reviewer", "agent": "claude"}]})
    with pytest.raises(RoleResolutionError, match="invalid_role_agents"):
        effective_role_mapping({"role_agents": {**VALID, "tester": "gemini"}})


def test_unreadable_state_refuses(tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{broken")
    with pytest.raises(RoleResolutionError, match="unreadable_state"):
        _load_role_to_agent_with_source(str(state))


def test_unknown_agent_has_no_implementer_fallback():
    with pytest.raises(RoleResolutionError, match="unknown_agent"):
        resolve_agent_role("codx")


@pytest.mark.parametrize("state", [
    {"role_agents": VALID},
    {"role_agents": {**VALID, "reviewer": "claude_cloud"}},
    {"roles": [{"role": "implementer", "agent": "codex"},
               {"role": "reviewer", "agent": "claude"}]},
])
def test_current_fleet_shapes_resolve(state):
    mapping, _ = effective_role_mapping(state)
    assert mapping == {**VALID, "reviewer": state.get("role_agents", VALID)["reviewer"]}


@pytest.mark.parametrize("state_body,reason", [
    (json.dumps({"role_agents": {"implementer": "codex"}}), "invalid_role_agents"),
    ("{broken", "unreadable_state"),
])
def test_bad_state_starts_but_refuses_dispatch_once(tmp_path, monkeypatch, state_body, reason):
    state = tmp_path / "state.json"
    state.write_text(state_body)
    notices = []
    monkeypatch.setattr("agent_crew.server._deliver_coordinator_notice",
                        lambda message: notices.append(message) or True)
    app = create_app(str(tmp_path / "tasks.db"), state_path=str(state),
                     pane_map={"implementer": "%100"},
                     push_fn=lambda *args: pytest.fail("dispatched under bad role map"),
                     watchdog_disabled=True, anomaly_disabled=True,
                     identity_required=False)
    with TestClient(app) as client:
        for i in range(2):
            response = client.post("/tasks", json={
                "task_id": "impl-role-refusal", "task_type": "implement",
                "description": "work", "branch": "main",
                "context": {"risk_tier": 1},
            }) if i == 0 else client.get("/tasks/next?role=implementer&agent=codex")
            assert response.status_code in (200, 201, 409)
    assert len(notices) == 1
    assert reason in notices[0]


def test_dispatcher_does_not_claim_override_under_invalid_roles(tmp_path, monkeypatch):
    """Checkout-only workers cannot bypass a refused project role map."""
    state = tmp_path / "state.json"
    checkout = tmp_path / "codex"
    checkout.mkdir()
    state.write_text(json.dumps({
        "project": "demo", "worktrees": {"codex": str(checkout)},
        "role_agents": {"implementer": "codex", "reviewer": "claude"},
    }))
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", "0.01")
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(
        task_id="impl-override-930", task_type="implement", description="work",
        project="demo", context={"agent_override": "codex", "risk_tier": 1}))
    claimed = threading.Event()
    original = TaskQueue.dequeue

    def observe_dequeue(self, *args, **kwargs):
        task = original(self, *args, **kwargs)
        if task is not None and task.task_id == "impl-override-930":
            claimed.set()
        return task

    monkeypatch.setattr(TaskQueue, "dequeue", observe_dequeue)
    app = create_app(str(tmp_path / "tasks.db"), state_path=str(state),
                     watchdog_disabled=True, anomaly_disabled=True,
                     identity_required=False)
    with TestClient(app):
        claimed.wait(0.5)
    assert not claimed.is_set()
    assert queue.get_task_status("impl-override-930") == "pending"


def test_no_default_role_map_or_agent_role_get_outside_resolver():
    root = Path(__file__).parents[2] / "src" / "agent_crew"
    for path in root.glob("*.py"):
        if path.name == "role_mapping.py":
            continue
        source = path.read_text()
        assert "DEFAULT_ROLE_TO_AGENT" not in source, path.name
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr == "get" and isinstance(node.func.value, ast.Name):
                assert not (node.func.value.id == "_AGENT_TO_ROLE" and len(node.args) > 1), path.name
