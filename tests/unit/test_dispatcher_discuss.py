"""Tests for dispatcher mode handling of discuss tasks.

Bugs caught:
- Dispatcher loop ignored task_type=discuss (tasks stuck in pending forever)
- claude -p --output-format stream-json missing --verbose flag (exit code 2)
"""
import asyncio
import inspect
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import agent_crew.server as srv_module
from agent_crew.queue import TaskQueue, TaskRequest, TaskResult
from agent_crew.server import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _discuss_payload(task_id: str, agent: str = "claude") -> dict:
    return {
        "task_id": task_id,
        "task_type": "discuss",
        "description": "Discuss: test topic",
        "branch": "main",
        "priority": 3,
        "context": {"agent": agent, "perspective": "analyst", "round": 1},
        "project": "test_project",
    }


# ---------------------------------------------------------------------------
# U-DD01: Dispatcher loop dispatches discuss tasks per agent
# ---------------------------------------------------------------------------

def test_u_dd01_dispatcher_loop_picks_up_discuss_tasks(tmp_db, tmp_path):
    """In dispatcher mode, pending discuss tasks must transition to in_progress
    within seconds — not remain stuck as 'pending' forever.

    Root cause of bug: _dispatcher_loop only looped over
    ('implementer','reviewer','tester') and never called
    dequeue_discuss_for_agent, so discuss tasks were never dispatched.
    """
    worktree_dir = tmp_path / "wt"
    worktree_dir.mkdir()
    (worktree_dir / ".git").mkdir()

    state = {
        "worktrees": {
            "claude": str(worktree_dir),
            "codex": str(worktree_dir),
            "gemini": str(worktree_dir),
        }
    }
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state))

    # Track which agents were dispatched
    dispatched_agents: list[str] = []

    async def fake_subprocess(*args, **kwargs):
        # Determine which agent based on command args
        cmd_args = list(args)
        cmd0 = str(cmd_args[0]) if cmd_args else ""
        # gemini role now dispatches via the `agy` binary (Antigravity CLI).
        if "gemini" in cmd0 or cmd0.endswith("/agy") or cmd0 == "agy":
            dispatched_agents.append("gemini")
        elif "codex" in cmd0:
            dispatched_agents.append("codex")
        else:
            dispatched_agents.append("claude")
        proc = MagicMock()
        proc.returncode = 0
        proc.kill = MagicMock()
        # Simulate instant success
        future = asyncio.get_event_loop().create_future()
        future.set_result(0)
        proc.wait = AsyncMock(return_value=0)
        return proc

    with patch.dict(os.environ, {
        "AGENT_CREW_DISPATCHER": "1",
        "AGENT_CREW_DISPATCH_INTERVAL": "0.05",
        "AGENT_CREW_WORKTREE_SYNC_DISABLED": "1",
    }):
        with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
            with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                app = create_app(
                    db_path=tmp_db,
                    pane_map={},
                    port=0,
                    state_path=str(state_file),
                    watchdog_disabled=True,
                    anomaly_disabled=True,
                )
                with TestClient(app) as client:
                    for agent in ("claude", "codex", "gemini"):
                        resp = client.post("/tasks", json=_discuss_payload(f"t_{agent}", agent))
                        assert resp.status_code == 201

                    # Let dispatcher loop run a few cycles
                    time.sleep(0.4)

    # Verify all 3 agents were dispatched
    assert "claude" in dispatched_agents, f"claude discuss task not dispatched; got {dispatched_agents}"
    assert "codex" in dispatched_agents, f"codex discuss task not dispatched; got {dispatched_agents}"
    assert "gemini" in dispatched_agents, f"gemini discuss task not dispatched; got {dispatched_agents}"


# ---------------------------------------------------------------------------
# U-DD02: claude dispatch command includes --verbose with stream-json
# ---------------------------------------------------------------------------

def test_u_dd02_claude_dispatch_cmd_includes_verbose():
    """_dispatch_task must pass --verbose when using --output-format stream-json.

    Root cause of bug: without --verbose, claude -p exits with:
      'When using --print, --output-format=stream-json requires --verbose'
    causing every claude discuss/implement task to fail immediately.
    """
    source = inspect.getsource(srv_module)
    lines = source.splitlines()

    # Find the line with stream-json and check that --verbose is nearby
    stream_json_lines = [i for i, l in enumerate(lines) if "stream-json" in l]
    assert stream_json_lines, "stream-json not found in server.py at all"

    for line_no in stream_json_lines:
        # Look in the 10 lines before stream-json for --verbose
        context_start = max(0, line_no - 10)
        context = "\n".join(lines[context_start : line_no + 3])
        if "claude" in context and "--print" not in context:
            assert "--verbose" in context, (
                f"--verbose missing near 'stream-json' at line {line_no + 1}:\n{context}\n\n"
                "claude -p --output-format stream-json requires --verbose flag"
            )


# ---------------------------------------------------------------------------
# U-DD03: discuss tasks dispatched concurrently (not blocked by each other)
# ---------------------------------------------------------------------------

def test_u_dd03_discuss_tasks_dequeue_per_agent_independently(tmp_db):
    """dequeue_discuss_for_agent must allow all three agents to dequeue
    simultaneously without blocking each other (separate in_progress slots)."""
    tq = TaskQueue(tmp_db)
    for agent in ("claude", "codex", "gemini"):
        tq.enqueue(TaskRequest(
            task_id=f"slot_{agent}",
            task_type="discuss",
            description="discuss",
            context={"agent": agent, "perspective": "analyst"},
        ))

    # All three can be dequeued at the same time
    t_claude = tq.dequeue_discuss_for_agent("claude")
    t_codex = tq.dequeue_discuss_for_agent("codex")
    t_gemini = tq.dequeue_discuss_for_agent("gemini")

    assert t_claude is not None, "claude discuss task should be dequeued"
    assert t_codex is not None, "codex discuss task should be dequeued"
    assert t_gemini is not None, "gemini discuss task should be dequeued"

    # All three in_progress — no cross-agent blocking
    assert tq.has_discuss_in_progress_for_agent("claude")
    assert tq.has_discuss_in_progress_for_agent("codex")
    assert tq.has_discuss_in_progress_for_agent("gemini")


# ---------------------------------------------------------------------------
# U-DD04: discuss tasks not mixed with role-based task slots
# ---------------------------------------------------------------------------

def test_u_dd04_discuss_slots_independent_from_role_slots(tmp_db):
    """A discuss task for 'claude' must not block the 'implementer' slot,
    and vice versa — they use different slot keys in the dispatcher."""
    tq = TaskQueue(tmp_db)

    # Enqueue a discuss task for claude and an implement task
    tq.enqueue(TaskRequest(
        task_id="discuss_1",
        task_type="discuss",
        description="discuss",
        context={"agent": "claude", "perspective": "analyst"},
    ))
    tq.enqueue(TaskRequest(
        task_id="impl_1",
        task_type="implement",
        description="implement",
        context={},
    ))

    # Can dequeue both independently
    discuss_task = tq.dequeue_discuss_for_agent("claude")
    impl_task = tq.dequeue(role="implementer")

    assert discuss_task is not None and discuss_task.task_id == "discuss_1"
    assert impl_task is not None and impl_task.task_id == "impl_1"
