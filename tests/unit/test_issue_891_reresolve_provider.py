"""#891: a provider bound before a role change must not claim the task after it."""

import json
import os
import sqlite3
import subprocess
import time

from fastapi.testclient import TestClient

import agent_crew.server as server
from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


def _state(path, implementer, reviewer):
    path.write_text(json.dumps({
        "project": "demo",
        "role_agents": {"implementer": implementer, "reviewer": reviewer, "tester": "script"},
        "worktrees": {"codex": "/wt/codex", "claude": "/wt/claude"},
    }))
    stamp = path.stat().st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(stamp, stamp))


def _claimed_by(db, task_id):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT claimed_by_agent, claimed_via FROM tasks WHERE task_id = ?",
                            (task_id,)).fetchone()
    finally:
        conn.close()


def _cloud_on(monkeypatch, launches):
    monkeypatch.setattr(cloud, "cloud_dispatch_enabled", lambda: True)
    monkeypatch.setattr(cloud, "cloud_shadow_enabled", lambda: False)
    monkeypatch.setattr(cloud, "probe_cloud_cli",
                        lambda *args, **kwargs: cloud.CloudCapability(True, "ok"))

    def run(argv, **kwargs):
        launches.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "refused in test")

    monkeypatch.setattr(cloud, "_default_run", run)


def test_reverted_implementer_role_reroutes_task_bound_to_cloud(tmp_path, monkeypatch):
    """Alpha 10-28: implementer reverted claude_cloud -> claude, the cloud kept
    the review seat, and an implement task bound earlier was claimed via
    cloud_push anyway."""
    state_path = tmp_path / "state.json"
    db = str(tmp_path / "tasks.db")
    _state(state_path, "claude_cloud", "claude_cloud")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", "0.02")
    launches = []
    _cloud_on(monkeypatch, launches)
    seen = []

    def cooldown(_self, agent):
        seen.append(agent)
        return time.time() + 60

    monkeypatch.setattr(server.QuotaBudgetProvider, "active_cooldown_until", cooldown)
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest(
        task_id="impl-891", task_type="implement", description="implement",
        branch="fix/891", priority=1, project="demo",
        context={"agent_override": "claude_cloud", "implementer_agent": "claude_cloud"}))
    queue.enqueue(TaskRequest(
        task_id="review-891", task_type="review", description="review",
        priority=2, project="demo", context={"implementer_agent": "codex"}))
    _state(state_path, "claude", "claude_cloud")

    app = server.create_app(db_path=db, state_path=str(state_path),
                            watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        deadline = time.monotonic() + 3
        while "claude" not in seen and time.monotonic() < deadline:
            time.sleep(0.01)

    assert _claimed_by(db, "impl-891")[0] != "claude_cloud"
    assert "claude" in seen
    task = queue.get_task("impl-891")
    assert (task.context or {}).get("agent_override") != "claude_cloud"


def test_current_cloud_implementer_keeps_its_binding(tmp_path):
    state_path = tmp_path / "state.json"
    _state(state_path, "claude_cloud", "codex")
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest(
        task_id="impl-891-keep", task_type="implement", description="implement",
        branch="fix/891", project="demo", context={"agent_override": "claude_cloud"}))
    app = server.create_app(db_path=db, state_path=str(state_path),
                            watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        assert app.state.reroute_stale_bindings() == []
    assert queue.get_task("impl-891-keep").context["agent_override"] == "claude_cloud"


def test_local_operator_override_is_not_rerouted(tmp_path):
    state_path = tmp_path / "state.json"
    _state(state_path, "codex", "claude_cloud")
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest(
        task_id="impl-891-local", task_type="implement", description="implement",
        branch="fix/891", project="demo", context={"agent_override": "claude"}))
    app = server.create_app(db_path=db, state_path=str(state_path),
                            watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        assert app.state.reroute_stale_bindings() == []
    assert queue.get_task("impl-891-local").context["agent_override"] == "claude"


def test_override_enqueued_after_sweep_is_not_claimed_by_cloud_review_seat(tmp_path):
    """Review of PR #916: the sweep ran (implementer=claude, reviewer=claude_cloud),
    then an implement task bound to claude_cloud arrived before the cloud
    reviewer's dequeue. The claim itself must refuse it, not the sweep."""
    state_path = tmp_path / "state.json"
    _state(state_path, "claude", "claude_cloud")
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    app = server.create_app(db_path=db, state_path=str(state_path),
                            watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        assert app.state.reroute_stale_bindings() == []
        queue.enqueue(TaskRequest(
            task_id="impl-891-race", task_type="implement", description="implement",
            branch="fix/891", project="demo", context={"agent_override": "claude_cloud"}))
        claimed = queue.dequeue(agent="claude_cloud", role="reviewer",
                                claimed_via="cloud_push", skip_deferred=True)
    assert claimed is None
    assert queue.get_task("impl-891-race").status == "pending"
    assert _claimed_by(db, "impl-891-race")[0] != "claude_cloud"


def test_cloud_review_seat_still_claims_its_own_bound_review(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(
        task_id="review-891-own", task_type="review", description="review",
        project="demo", context={"agent_override": "claude_cloud"}))
    claimed = queue.dequeue(agent="claude_cloud", role="reviewer", claimed_via="cloud_push")
    assert claimed is not None and claimed.task_id == "review-891-own"
