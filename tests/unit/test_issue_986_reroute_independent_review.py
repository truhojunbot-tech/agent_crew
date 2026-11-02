"""#986: the #891 reroute must not hand a review to its implementer's provider."""

import json
import os

from fastapi.testclient import TestClient

import agent_crew.server as server
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


def _review(queue, task_id, implementer):
    queue.enqueue(TaskRequest(
        task_id=task_id, task_type="review", description="review", project="demo",
        context={"agent_override": "claude_cloud", "implementer_agent": implementer}))


def test_codex_implemented_review_is_not_rerouted_to_codex_reviewer(tmp_path):
    """apify-forge 8109, 11-02: reviewer=codex, impl by codex, review bound to
    claude_cloud. Rerouting gave it to codex -> reviewer_matches_implementer."""
    state_path = tmp_path / "state.json"
    _state(state_path, "claude_cloud", "codex")
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _review(queue, "review-986", "codex")
    app = server.create_app(db_path=str(tmp_path / "tasks.db"), state_path=str(state_path),
                            watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        assert app.state.reroute_stale_bindings() == []
        reason = queue.get_task("review-986").context["reroute_held_reason"]
        # A second sweep neither reroutes nor rewrites the typed reason.
        assert app.state.reroute_stale_bindings() == []
        assert queue.dequeue(agent="codex", role="reviewer", claimed_via="tmux_push") is None
    task = queue.get_task("review-986")
    assert task.status == "pending"
    assert task.context["agent_override"] == "claude_cloud"
    assert "rerouted_from_provider" not in task.context
    assert reason.startswith("no_independent_reviewer")
    assert "codex" in reason
    assert task.context["reroute_held_reason"] == reason


def test_independent_stale_review_still_reroutes(tmp_path):
    """#891 baseline: the role holder differs from the implementer -> reroute."""
    state_path = tmp_path / "state.json"
    _state(state_path, "claude_cloud", "codex")
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _review(queue, "review-986-ok", "claude")
    app = server.create_app(db_path=str(tmp_path / "tasks.db"), state_path=str(state_path),
                            watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        assert app.state.reroute_stale_bindings() == ["review-986-ok"]
        claimed = queue.dequeue(agent="codex", role="reviewer", claimed_via="tmux_push")
    assert claimed is not None and claimed.task_id == "review-986-ok"
    assert "agent_override" not in claimed.context
    assert claimed.context["rerouted_from_provider"] == "claude_cloud"
    assert "reroute_held_reason" not in claimed.context


def test_queue_guard_uses_provider_family(tmp_path):
    """claude_cloud and claude are one family: a claude-implemented review is
    not rerouted onto a claude reviewer either."""
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _review(queue, "review-986-family", "claude_cloud")
    _review(queue, "review-986-other", "codex")
    moved = queue.reroute_stale_provider_bindings("claude_cloud", (), reviewer_holder="claude")
    assert moved == ["review-986-other"]
    assert queue.get_task("review-986-family").context["agent_override"] == "claude_cloud"
    # Without a known reviewer holder the #891 behaviour is unchanged.
    assert queue.reroute_stale_provider_bindings("claude_cloud", ()) == ["review-986-family"]
