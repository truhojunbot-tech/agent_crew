"""#889: one role -> allowed-providers policy; gemini never implements."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from agent_crew import server
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.role_mapping import (
    ROLE_PROVIDER_POLICY, anonymous_pull_refusal, provider_family, role_policy,
    role_policy_refusal,
)

ROLES = {"implementer": "codex", "reviewer": "claude", "tester": "script"}


@pytest.mark.parametrize("agent", ["gemini", "gemini_cli", "agy", " Gemini-2 ", "script"])
def test_policy_refuses_non_implementers(agent):
    assert role_policy_refusal("implementer", agent)


@pytest.mark.parametrize("agent", ["claude", "codex", "claude_cloud"])
def test_policy_allows_implementers(agent):
    assert role_policy_refusal("implementer", agent) is None


@pytest.mark.parametrize("agent", ["script"])
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
    # project= pins the server identity; without it an inherited
    # AGENT_CREW_PROJECT answers the "demo" posts with 409 (review of #906).
    kwargs.setdefault("project", "demo")
    return server.create_app(db_path=str(tmp_path / "tasks.db"), state_path=str(state),
                             watchdog_disabled=True, anomaly_disabled=True, **kwargs)


def _post(client, task_id, task_type, context=None, branch="agent/x"):
    payload = {
        "task_id": task_id, "task_type": task_type, "description": "d",
        "branch": branch, "project": "demo",
        "context": {"risk_tier": 1, **(context or {})}}
    if task_type in {"review", "test"}:
        payload["pr_number"] = 889
        payload["context"]["reviewed_sha"] = "a" * 40
    return client.post("/tasks", headers={"X-Agent-Crew-Project": "demo"}, json=payload)


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


def test_enqueue_refuses_gemini_test_and_accepts_script_test_and_codex_implement(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        assert _post(client, "test-gemini-889", "test",
                     {"agent_override": "gemini"}).status_code == 422
        assert _post(client, "test-script-889", "test",
                     {"agent_override": "script"}).status_code == 201
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


def _queued_implement(db):
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest(task_id="impl-pull-889", task_type="implement",
                              description="impl", branch="agent/889", project="demo"))
    return queue


def test_http_pull_refuses_implement_for_gemini(tmp_path, monkeypatch):
    """GET /tasks/next is a delivery route: gemini never receives implement work."""
    monkeypatch.setenv("AGENT_CREW_DELIVERY", "push")
    app = _app(tmp_path)
    with TestClient(app) as client:
        queue = _queued_implement(str(tmp_path / "tasks.db"))
        resp = client.get("/tasks/next", params={"agent": "gemini", "role": "implementer"},
                          headers={"X-Agent-Crew-Project": "demo"})
    assert resp.status_code == 409, resp.text
    assert "role_policy" in resp.text
    result = queue.get_result("impl-pull-889")
    assert result is not None and result.status == "needs_human"
    assert "role_policy" in result.summary


def test_http_pull_delivers_implement_to_codex(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DELIVERY", "push")
    with TestClient(_app(tmp_path)) as client:
        _queued_implement(str(tmp_path / "tasks.db"))
        resp = client.get("/tasks/next", params={"agent": "codex", "role": "implementer"},
                          headers={"X-Agent-Crew-Project": "demo"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task_id"] == "impl-pull-889"


def _mcp_get_next(db, **kwargs):
    mcp_server = pytest.importorskip("agent_crew.mcp_server")
    try:
        mcp = mcp_server.build_mcp_server(db)
    except RuntimeError as exc:
        pytest.skip(str(exc))
    return mcp._tool_manager._tools["get_next_task"].fn(**kwargs)


def test_mcp_pull_refuses_implement_for_gemini(tmp_path):
    db = str(tmp_path / "tasks.db")
    queue = _queued_implement(db)
    payload = _mcp_get_next(db, agent="gemini", role="implementer")
    assert payload["task_id"] == "impl-pull-889"
    assert "role_policy" in payload["dispatch_refused"]
    assert "description" not in payload
    result = queue.get_result("impl-pull-889")
    assert result is not None and result.status == "needs_human"


def test_mcp_pull_delivers_implement_to_codex(tmp_path):
    db = str(tmp_path / "tasks.db")
    _queued_implement(db)
    payload = _mcp_get_next(db, agent="codex", role="implementer")
    assert payload["task_id"] == "impl-pull-889"
    assert "dispatch_refused" not in payload


@pytest.mark.parametrize("role", ["implementer", "coder", "tester", ""])
def test_anonymous_pull_refusal_for_restricted_or_unscoped_roles(role):
    assert anonymous_pull_refusal(role, "")
    assert anonymous_pull_refusal(role, "  ")


def test_anonymous_pull_allowed_for_unrestricted_role_and_named_agents():
    assert anonymous_pull_refusal("reviewer", "") is None
    assert anonymous_pull_refusal("implementer", "codex") is None


def test_anonymous_discussion_pull_is_allowed():
    # E2E discussion stubs pull role=panel without naming a provider; no
    # family is barred from discussion work, so the pull must not be refused.
    assert anonymous_pull_refusal("panel", "") is None


@pytest.mark.parametrize("params", [{"role": "implementer"}, {}, {"role": "implementer", "agent": " "}])
def test_http_pull_refuses_anonymous_implement_without_claiming(tmp_path, monkeypatch, params):
    """#889: a blank agent has no provider family, so it may not pull restricted work."""
    monkeypatch.setenv("AGENT_CREW_DELIVERY", "push")
    with TestClient(_app(tmp_path)) as client:
        queue = _queued_implement(str(tmp_path / "tasks.db"))
        resp = client.get("/tasks/next", params=params,
                          headers={"X-Agent-Crew-Project": "demo"})
    assert resp.status_code == 409, resp.text
    assert "role_policy" in resp.text
    assert "dispatch_nonce" not in resp.text
    # Refused before the claim: the task stays pending for a named provider.
    assert queue.get_task_status("impl-pull-889") == "pending"


@pytest.mark.parametrize("kwargs", [{"role": "implementer"}, {}, {"agent": "", "role": "tester"}])
def test_mcp_pull_refuses_anonymous_implement_without_claiming(tmp_path, kwargs):
    db = str(tmp_path / "tasks.db")
    queue = _queued_implement(db)
    payload = _mcp_get_next(db, **kwargs)
    assert "role_policy" in payload["dispatch_refused"]
    assert "description" not in payload and "dispatch_nonce" not in payload
    assert queue.get_task_status("impl-pull-889") == "pending"
