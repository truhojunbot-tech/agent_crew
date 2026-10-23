"""#834: role changes take effect before the next provider dispatch."""

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

import agent_crew.server as server
from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


def _state(path, reviewer):
    path.write_text(json.dumps({
        "project": "demo",
        "role_agents": {"implementer": "codex", "reviewer": reviewer, "tester": "script"},
        "worktrees": {"codex": "/wt/codex", "claude": "/wt/claude"},
    }))
    stamp = path.stat().st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(stamp, stamp))


def _app(tmp_path, state_path, **kwargs):
    return server.create_app(
        db_path=str(tmp_path / "tasks.db"), state_path=str(state_path),
        watchdog_disabled=True, anomaly_disabled=True, **kwargs)


def test_changed_state_routes_next_review_to_new_provider_worktree(tmp_path, caplog):
    state_path = tmp_path / "state.json"
    _state(state_path, "codex")
    app = _app(tmp_path, state_path)
    review = TaskRequest(task_id="review-834", task_type="review",
                         description="review", project="demo")
    assert app.state.resolve_dispatch_target(review, "reviewer") == (
        "codex", "/wt/codex")

    _state(state_path, "claude")
    assert app.state.resolve_dispatch_target(review, "reviewer") == (
        "claude", "/wt/claude")
    assert "effective role map at startup" in caplog.text
    assert "state changed; effective role map" in caplog.text


def test_unchanged_mtime_does_not_reload_role_map(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    _state(state_path, "claude")
    app = _app(tmp_path, state_path)
    reads = []
    original = server._load_role_to_agent
    original_worktrees = server._load_worktree_map

    def counted(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(server, "_load_role_to_agent", counted)
    monkeypatch.setattr(server, "_load_worktree_map",
                        lambda path: reads.append("worktrees") or original_worktrees(path))
    review = TaskRequest(task_id="review-834", task_type="review",
                         description="review", project="demo")
    for _ in range(3):
        assert app.state.resolve_dispatch_target(review, "reviewer")[0] == "claude"
    assert reads == []


def test_dispatcher_uses_changed_role_on_next_claim(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    _state(state_path, "codex")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", "0.02")
    seen = []

    def cooldown(_self, agent):
        seen.append(agent)
        return time.time() + 60

    monkeypatch.setattr(server.QuotaBudgetProvider, "active_cooldown_until", cooldown)
    app = _app(tmp_path, state_path)
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    with TestClient(app):
        _state(state_path, "claude")
        queue.enqueue(TaskRequest(
            task_id="review-live-834", task_type="review", description="review",
            project="demo", context={"implementer_agent": "codex"}))
        deadline = time.monotonic() + 3
        while "claude" not in seen and time.monotonic() < deadline:
            time.sleep(0.01)
    assert "claude" in seen
    assert "codex" not in seen


def test_cloud_review_guard_stops_before_provider_launch(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(
        task_id="review-cloud-834", task_type="review", description="review",
        project="demo", context={"implementer_agent": "claude_cloud"}))
    monkeypatch.setattr(cloud, "cloud_dispatch_enabled", lambda: True)
    monkeypatch.setattr(cloud, "probe_cloud_cli",
                        lambda *args, **kwargs: cloud.CloudCapability(True, "ok"))

    def guard(task, agent):
        assert agent == "claude_cloud"
        return queue.mark_needs_human(task.task_id, "reviewer_matches_implementer", {})

    outcome = cloud.dispatch_cloud_for_role(
        queue, role="reviewer", task_type="review", review_guard_fn=guard,
        run_fn=lambda *args, **kwargs: pytest.fail("provider launched"))
    assert outcome.launch_error == "reviewer_matches_implementer"
    assert queue.get_result("review-cloud-834").status == "needs_human"


@pytest.mark.asyncio
async def test_review_is_refused_when_resolved_reviewer_is_implementer(tmp_path):
    state_path = tmp_path / "state.json"
    _state(state_path, "claude")
    notices = []
    app = _app(tmp_path, state_path,
               coordinator_notify_fn=lambda message: notices.append(message) or True)
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(
        task_id="review-834", task_type="review", description="review",
        project="demo", context={"implementer_agent": "codex"}))
    review = queue.dequeue(role="reviewer")

    _state(state_path, "codex")
    with TestClient(app):
        await app.state.dispatch_task(review, "reviewer")

    result = queue.get_result(review.task_id)
    assert result.status == "needs_human"
    assert result.summary.startswith("reviewer_matches_implementer")
    assert "codex" in result.summary
    assert notices
