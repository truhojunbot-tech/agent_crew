"""#924b: Gemini is Athena-only at every entry point, not just the test role."""

import ast
import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_crew import discussion, fallback, role_mapping, server
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.role_mapping import (
    GEMINI_ATHENA_ONLY, RoleResolutionError, effective_role_mapping,
    gemini_allowed_for_project, is_gemini_family,
)

pytestmark = pytest.mark.gemini_policy

GEMINI_NAMES = ["gemini", "gemini-2.5-pro", "GEMINI", "agy", " Agy "]
TASK_TYPES = ["implement", "review", "test", "discuss"]


def _queue(tmp_path, project):
    """A queue whose own identity (state.json + directory) is ``project``."""
    base = tmp_path / project
    base.mkdir()
    (base / "state.json").write_text(json.dumps({"project": project}))
    return TaskQueue(str(base / "tasks.db"))


def _task(task_id, task_type, **context):
    return TaskRequest(task_id=task_id, task_type=task_type, description=task_id,
                       branch="main", context={"risk_tier": 1, **context})


def _refused(exc_info):
    assert exc_info.value.reason == GEMINI_ATHENA_ONLY


def test_allowlist_constant_is_exactly_athena():
    """The production allowlist cannot leak a wider set (#924c)."""
    assert role_mapping.GEMINI_ALLOWED_PROJECTS == frozenset({"athena"})
    assert gemini_allowed_for_project("athena")
    assert gemini_allowed_for_project(" Athena ")
    for project in ("agent_crew", "alpha", "", None, "athena-dev"):
        assert not gemini_allowed_for_project(project)


def test_production_module_source_pins_allowlist_to_athena():
    """Read the shipped module itself, untouched by any fixture or import state."""
    source = Path(role_mapping.__file__).read_text(encoding="utf-8")
    module = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "GEMINI_ALLOWED_PROJECTS" for t in node.targets):
            module["allowlist"] = eval(compile(ast.Expression(node.value), "<allowlist>", "eval"),
                                       {"frozenset": frozenset})
    assert module["allowlist"] == frozenset({"athena"})
    func = next(n for n in ast.parse(source).body
                if isinstance(n, ast.FunctionDef) and n.name == "gemini_allowed_for_project")
    body = ast.get_source_segment(source, func) or ""
    assert "environ" not in body and "getenv" not in body


@pytest.mark.parametrize("name", GEMINI_NAMES)
def test_gemini_family_helper(name):
    assert is_gemini_family(name)


@pytest.mark.parametrize("name", ["claude", "codex", "script", "", None])
def test_non_gemini_is_not_family(name):
    assert not is_gemini_family(name)


@pytest.mark.parametrize("task_type", TASK_TYPES)
@pytest.mark.parametrize("name", ["gemini", "agy"])
def test_agent_override_refused_outside_athena(tmp_path, task_type, name):
    queue = _queue(tmp_path, "agent_crew")
    with pytest.raises(RoleResolutionError) as exc:
        queue.enqueue(_task("t-1", task_type, agent_override=name))
    _refused(exc)
    assert queue.get_task("t-1") is None


@pytest.mark.parametrize("task_type", TASK_TYPES)
def test_agent_override_accepted_on_athena(tmp_path, task_type):
    queue = _queue(tmp_path, "athena")
    try:
        queue.enqueue(_task("t-1", task_type, agent_override="gemini"))
    except RoleResolutionError as exc:
        assert exc.reason != GEMINI_ATHENA_ONLY


def test_context_project_athena_does_not_unlock_non_athena_queue(tmp_path):
    """The project is the queue's own identity, never the task's claim."""
    queue = _queue(tmp_path, "agent_crew")
    with pytest.raises(RoleResolutionError) as exc:
        queue.enqueue(_task("t-1", "review", agent_override="gemini", project="athena"))
    _refused(exc)
    request = _task("t-2", "review", agent_override="gemini")
    request.project = "athena"
    with pytest.raises(Exception) as exc2:
        queue.enqueue(request)
    assert queue.get_task("t-2") is None
    assert GEMINI_ATHENA_ONLY in str(getattr(exc2.value, "reason", exc2.value)) \
        or "PROJECT_MISMATCH" in str(exc2.value)


@pytest.mark.parametrize("agents", [["gemini"], ["claude", "gemini"], ["codex", "agy"]])
def test_explicit_discuss_panel_refused_whole(tmp_path, agents):
    queue = _queue(tmp_path, "agent_crew")
    with pytest.raises(RoleResolutionError) as exc:
        discussion.enqueue_panel_tasks(queue, agents, "topic", {"risk_tier": 1})
    _refused(exc)
    assert queue.list_tasks() == []


@pytest.mark.parametrize("key", ["agent", "agents", "panel", "panel_agents"])
def test_discuss_context_agent_lists_refused(tmp_path, key):
    queue = _queue(tmp_path, "agent_crew")
    value = "gemini" if key == "agent" else ["claude", "gemini"]
    with pytest.raises(RoleResolutionError) as exc:
        queue.enqueue(_task("d-1", "discuss", **{key: value}))
    _refused(exc)


def test_discuss_panel_with_gemini_accepted_on_athena(tmp_path):
    queue = _queue(tmp_path, "athena")
    try:
        discussion.enqueue_panel_tasks(queue, ["claude", "gemini"], "topic", {"risk_tier": 1})
    except RoleResolutionError as exc:
        assert exc.reason != GEMINI_ATHENA_ONLY


@pytest.mark.parametrize("key", ["fallback_chain", "fallback_agents"])
def test_fallback_chain_in_context_refused(tmp_path, key):
    queue = _queue(tmp_path, "agent_crew")
    with pytest.raises(RoleResolutionError) as exc:
        queue.enqueue(_task("f-1", "implement", **{key: ["claude", "gemini"]}))
    _refused(exc)


@pytest.mark.parametrize("project,refused", [("agent_crew", True), ("athena", False)])
def test_fallback_chain_config_and_next_agent(tmp_path, project, refused):
    base = tmp_path / project
    base.mkdir()
    state = base / "state.json"
    state.write_text(json.dumps({"project": project}))
    (base / "fallback_chains.json").write_text(json.dumps({"review": ["claude", "gemini"]}))
    chains = {"review": ["claude", "gemini"]}
    if refused:
        with pytest.raises(RoleResolutionError) as exc:
            fallback.load_fallback_chains(str(state))
        _refused(exc)
        with pytest.raises(RoleResolutionError) as exc:
            fallback.next_agent("review", "claude", [], chains=chains, project=project)
        _refused(exc)
    else:
        assert fallback.load_fallback_chains(str(state))["review"] == ["claude", "gemini"]
        assert fallback.next_agent("review", "claude", [], chains=chains, project=project) == "gemini"


@pytest.mark.parametrize("state", [
    {"role_agents": {"implementer": "codex", "reviewer": "gemini", "tester": "script"}},
    {"roles": [{"role": "reviewer", "agent": "agy"}]},
])
def test_role_config_refused_outside_athena(state):
    with pytest.raises(RoleResolutionError) as exc:
        effective_role_mapping(state, project="agent_crew")
    _refused(exc)
    mapping, _source = effective_role_mapping(state, project="athena")
    assert is_gemini_family(mapping["reviewer"])


@pytest.mark.parametrize("name", ["gemini", "agy"])
def test_dequeue_gemini_claims_nothing_outside_athena(tmp_path, name):
    queue = _queue(tmp_path, "agent_crew")
    queue.enqueue(_task("r-1", "review"))
    assert queue.dequeue(agent=name) is None
    assert queue.dequeue(agent=name, override_task_type="review") is None
    assert queue.get_task("r-1").status == "pending"


def test_dequeue_gemini_still_claims_on_athena(tmp_path):
    queue = _queue(tmp_path, "athena")
    queue.enqueue(_task("r-1", "review", agent_override="gemini"))
    claimed = queue.dequeue(agent="gemini", override_task_type="review")
    assert claimed is not None and claimed.task_id == "r-1"


def test_dispatch_backstop_refuses_gemini_without_provider_call(tmp_path, monkeypatch):
    """Cascade/failover paths that skip enqueue still stop at dispatch."""
    base = tmp_path / "demo"
    base.mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    state = base / "state.json"
    state.write_text(json.dumps({
        "project": "demo",
        "role_agents": {"implementer": "codex", "reviewer": "claude", "tester": "script"},
        "worktrees": {a: str(worktree) for a in ("claude", "codex", "gemini")}}))
    spawned = []

    async def fake_exec(*cmd, **_kwargs):
        spawned.append(cmd)
        raise AssertionError("refused task must never run")

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_exec)
    db = str(base / "tasks.db")
    app = server.create_app(db_path=db, state_path=str(state), pane_map={},
                            project="demo", watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        queue = TaskQueue(db)
        queue.enqueue(TaskRequest(task_id="impl-924b", task_type="implement",
                                  description="impl", branch="agent/924b", project="demo"))
        claimed = queue.dequeue(role="implementer")
        assert claimed is not None
        asyncio.run(app.state.dispatch_task(claimed, "implementer", dispatch_agent="gemini"))
        result = queue.get_result("impl-924b")
    assert spawned == []
    assert result is not None and result.status in ("failed", "needs_human")
    assert GEMINI_ATHENA_ONLY in json.dumps(vars(result), default=str)
