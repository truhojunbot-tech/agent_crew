"""#889: one role -> allowed-providers policy; gemini never implements."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from agent_crew import server
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.role_mapping import (
    ROLE_PROVIDER_POLICY, provider_family, role_policy, role_policy_refusal,
)

ROLES = {"implementer": "codex", "reviewer": "claude", "tester": "gemini"}


@pytest.mark.parametrize("agent", ["gemini", "gemini_cli", "agy", " Gemini-2 ", "script"])
def test_policy_refuses_non_implementers(agent):
    assert role_policy_refusal("implementer", agent)


@pytest.mark.parametrize("agent", ["claude", "codex", "claude_cloud"])
def test_policy_allows_implementers(agent):
    assert role_policy_refusal("implementer", agent) is None


@pytest.mark.parametrize("agent", ["gemini", "agy", "script"])
def test_policy_allows_testers(agent):
    assert role_policy_refusal("tester", agent) is None


def test_agy_is_gemini_family():
    assert provider_family("agy") == "gemini"


def test_policy_is_exposed_as_json():
    assert role_policy() == {role: (list(v) if v is not None else None)
                             for role, v in ROLE_PROVIDER_POLICY.items()}
    assert role_policy()["implementer"] == ["claude", "codex"]


def _app(tmp_path, roles=ROLES, **kwargs):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"project": "demo", "role_agents": roles}))
    return server.create_app(db_path=str(tmp_path / "tasks.db"), state_path=str(state),
                             watchdog_disabled=True, anomaly_disabled=True, **kwargs)


def _post(client, task_id, task_type, context=None, branch="agent/x"):
    return client.post("/tasks", headers={"X-Agent-Crew-Project": "demo"}, json={
        "task_id": task_id, "task_type": task_type, "description": "d",
        "branch": branch, "project": "demo",
        "context": {"risk_tier": 1, **(context or {})}})


@pytest.mark.parametrize("context", [
    {"agent_override": "gemini"},
    {"implementer_agent": "gemini"},
    {"agent_override": "gemini", "implementer_agent": "gemini"},
    {"agent_override": "gemini_cli"},
])
def test_enqueue_refuses_gemini_implement(tmp_path, context):
    with TestClient(_app(tmp_path)) as client:
        resp = _post(client, "impl-889", "implement", context)
    assert resp.status_code == 422, resp.text
    assert "role_policy" in resp.text


def test_enqueue_refuses_implement_when_role_is_gemini(tmp_path):
    roles = {"implementer": "gemini", "reviewer": "claude", "tester": "script"}
    with TestClient(_app(tmp_path, roles=roles)) as client:
        resp = _post(client, "impl-889", "implement")
    assert resp.status_code == 422, resp.text
    assert "role_policy" in resp.text


def test_branch_prefix_does_not_route(tmp_path):
    """A branch named for gemini is a label: the task goes to the role default."""
    app = _app(tmp_path)
    with TestClient(app) as client:
        resp = _post(client, "impl-889", "implement", branch="agent/gemini-cli/x")
        assert resp.status_code == 201, resp.text
        task = TaskQueue(str(tmp_path / "tasks.db")).get_task("impl-889")
        agent, _wt = app.state.resolve_dispatch_target(task, "implementer")
    assert agent == "codex"


def test_enqueue_accepts_gemini_test_and_codex_implement(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        assert _post(client, "test-889", "test",
                     {"agent_override": "gemini"}).status_code == 201
        assert _post(client, "impl-889", "implement",
                     {"agent_override": "codex"}).status_code == 201


def test_policy_endpoint(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        resp = client.get("/policy/roles")
    assert resp.status_code == 200
    assert resp.json()["roles"] == role_policy()
    assert resp.json()["task_type_to_role"]["implement"] == "implementer"


def _dispatch(tmp_path, monkeypatch, task, roles=ROLES):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "project": "demo", "role_agents": roles,
        "worktrees": {agent: str(worktree) for agent in ("claude", "codex", "gemini")}}))
    spawned = []

    async def fake_exec(*cmd, **_kwargs):
        spawned.append(cmd)
        raise AssertionError("refused task must never run")

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_exec)
    db = str(tmp_path / "tasks.db")
    app = server.create_app(db_path=db, state_path=str(state), pane_map={},
                            project="demo", watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        queue = TaskQueue(db)
        queue.enqueue(task)
        claimed = queue.dequeue(role="implementer")
        assert claimed is not None
        asyncio.run(app.state.dispatch_task(claimed, "implementer"))
        result = queue.get_result(task.task_id)
    assert spawned == []
    assert result is not None and result.status == "needs_human"
    assert "role_policy" in result.summary


def test_dispatch_refuses_fix_round_routed_to_gemini(tmp_path, monkeypatch):
    """Cascade-built fix rounds bypass HTTP admission; dispatch still refuses."""
    _dispatch(tmp_path, monkeypatch, TaskRequest(
        task_id="fix-review-889-r1", task_type="implement", description="fix",
        branch="agent/889", project="demo",
        context={"agent_override": "gemini", "implementer_agent": "gemini",
                 "fix_round": 1, "prev_task_id": "review-889"}))


def test_dispatch_refuses_role_default_gemini(tmp_path, monkeypatch):
    _dispatch(tmp_path, monkeypatch, TaskRequest(
        task_id="impl-889", task_type="implement", description="impl",
        branch="agent/889", project="demo"),
        roles={"implementer": "gemini", "reviewer": "claude", "tester": "script"})


def test_dispatch_refuses_explicit_dispatch_agent(tmp_path, monkeypatch):
    """A failover/slot swap hands ``dispatch_agent`` straight to dispatch."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"project": "demo", "role_agents": ROLES,
                                 "worktrees": {"codex": str(worktree)}}))

    async def fake_exec(*_cmd, **_kwargs):
        raise AssertionError("refused task must never run")

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_exec)
    db = str(tmp_path / "tasks.db")
    app = server.create_app(db_path=db, state_path=str(state), pane_map={},
                            project="demo", watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        queue = TaskQueue(db)
        queue.enqueue(TaskRequest(task_id="retry-889", task_type="implement",
                                  description="retry", branch="agent/889", project="demo"))
        asyncio.run(app.state.dispatch_task(queue.dequeue(role="implementer"),
                                            "implementer", dispatch_agent="gemini"))
        result = queue.get_result("retry-889")
    assert result is not None and result.status == "needs_human"
