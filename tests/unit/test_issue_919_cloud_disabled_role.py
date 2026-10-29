"""#919: claude_cloud role with cloud dispatch disabled must not stay silent."""

import json
import logging
import threading
import time

from fastapi.testclient import TestClient

from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import _load_worktree_map, create_app


def _state(tmp_path, implementer_worktree=True):
    roles = []
    for role, agent in (("implementer", "claude_cloud"), ("reviewer", "codex"),
                        ("tester", "gemini")):
        if role == "implementer" and not implementer_worktree:
            continue
        wt = tmp_path / role
        wt.mkdir()
        (wt / ".git").mkdir()
        roles.append({"role": role, "agent": agent, "worktree": str(wt)})
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"project": "agent_crew", "role_agents": {
        "implementer": "claude_cloud", "reviewer": "codex", "tester": "gemini"},
        "roles": roles}))
    return str(path)


def _wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.02)
    assert predicate()


def test_disabled_cloud_implementer_warns_and_notices_once_per_task(tmp_path, monkeypatch, caplog):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    for task_id in ("impl-919a", "impl-919b"):
        queue.enqueue(TaskRequest(task_id=task_id, task_type="implement",
                                  description="Implement #919", branch=f"agent/{task_id}",
                                  project="agent_crew", context={"repo": "owner/repo"}))
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", ".02")
    monkeypatch.delenv("AGENT_CREW_CLOUD_ENABLED", raising=False)

    calls = []
    calls_lock = threading.Lock()
    real_dispatch = cloud.dispatch_cloud_for_role

    def counting_dispatch(*args, **kwargs):
        with calls_lock:
            calls.append(kwargs.get("role"))
        return real_dispatch(*args, **kwargs)

    monkeypatch.setattr(cloud, "dispatch_cloud_for_role", counting_dispatch)
    notices = []
    caplog.set_level(logging.WARNING, logger="agent_crew.server")
    app = create_app(queue._db_path, state_path=_state(tmp_path),
                     watchdog_disabled=True, anomaly_disabled=True,
                     coordinator_notify_fn=lambda message: notices.append(message) or True)
    with TestClient(app):
        # Many ticks must still yield one warning and one notice per task.
        _wait_for(lambda: calls.count("implementer") >= 6)

    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "cloud_disabled_role" in r.getMessage()]
    assert sorted(w.getMessage().split("task=")[1].split()[0] for w in warnings) == [
        "impl-919a", "impl-919b"]
    assert len(notices) == 2
    assert all("kind=cloud_disabled_role" in n for n in notices)
    assert {n.split("task=")[1].split()[0] for n in notices} == {"impl-919a", "impl-919b"}
    # The task stays pending: no silent remap to another provider.
    assert queue.get_task("impl-919a").status == "pending"
    assert queue.get_task("impl-919b").status == "pending"


def test_claude_cloud_role_has_no_worktree_map_warning(tmp_path, caplog):
    state_path = _state(tmp_path, implementer_worktree=False)
    caplog.set_level(logging.WARNING, logger="agent_crew.server")
    result = _load_worktree_map(state_path)
    assert "implementer" not in result
    assert not [r for r in caplog.records if "no worktree; omitting role" in r.getMessage()]


def test_legacy_schema_claude_cloud_role_has_no_worktree_map_warning(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"project": "agent_crew",
                                "role_agents": {"implementer": "claude_cloud",
                                                "reviewer": "codex", "tester": "gemini"},
                                "worktrees": {"codex": "/wt/codex", "gemini": "/wt/gemini"}}))
    caplog.set_level(logging.WARNING, logger="agent_crew.server")
    assert _load_worktree_map(str(path)) == {"reviewer": "/wt/codex", "tester": "/wt/gemini"}
    assert not [r for r in caplog.records if "no worktree; omitting role" in r.getMessage()]


def test_non_cloud_role_without_worktree_still_warns(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"project": "agent_crew",
                                "role_agents": {"implementer": "claude",
                                                "reviewer": "codex", "tester": "gemini"},
                                "worktrees": {"codex": "/wt/codex", "gemini": "/wt/gemini"}}))
    caplog.set_level(logging.WARNING, logger="agent_crew.server")
    _load_worktree_map(str(path))
    assert [r for r in caplog.records if "no worktree; omitting role" in r.getMessage()]
