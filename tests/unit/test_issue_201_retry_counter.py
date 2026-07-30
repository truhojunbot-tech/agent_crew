"""Regression test for issue #201.

Bug: ``_transient_retries[task_id]`` was popped in the ``finally`` block on
*every* exit from ``_dispatch_task``, including the early ``return`` taken
right after a successful requeue. Since a requeued task re-enters
``_dispatch_task`` as a fresh call, the counter was always missing and
recomputed as 1 — so ``_n <= _MAX_TRANSIENT_RETRY`` was true forever and a
persistently-failing transient error retried indefinitely instead of
eventually failing with a clear "gave up after N retries" reason.

Fix: only pop the counter on a terminal outcome (``_terminal`` flag, set to
False right before the requeue-success ``return``).
"""
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _test_payload(task_id: str) -> dict:
    return {
        "task_id": task_id,
        "task_type": "test",
        "description": "Test PR #1",
        "branch": "main",
        "priority": 3,
        "context": {},
        "project": "test_project",
    }


def test_u_i201_transient_retry_counter_caps_and_fails(tmp_db, tmp_path):
    """A task that always produces a retriable transient error must give up
    after AGENT_CREW_TRANSIENT_RETRY_MAX attempts, not retry forever."""
    wt_claude = tmp_path / "claude"
    wt_codex = tmp_path / "codex"
    wt_gemini = tmp_path / "gemini"
    for wt in (wt_claude, wt_codex, wt_gemini):
        wt.mkdir()
        (wt / ".git").mkdir()

    state = {
        "worktrees": {
            "claude": str(wt_claude),
            "codex": str(wt_codex),
            "gemini": str(wt_gemini),
        }
    }
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state))

    spawn_count = 0

    async def fake_subprocess(*args, **kwargs):
        nonlocal spawn_count
        spawn_count += 1
        stdout = kwargs.get("stdout")
        if stdout is not None:
            stdout.write(b"Error: timeout waiting for response\n")
            stdout.flush()
        proc = MagicMock()
        proc.returncode = 1
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=1)
        return proc

    with patch.dict(os.environ, {
        "AGENT_CREW_DISPATCHER": "1",
        "AGENT_CREW_DISPATCH_INTERVAL": "0.05",
        "AGENT_CREW_WORKTREE_SYNC_DISABLED": "1",
        "AGENT_CREW_TRANSIENT_RETRY_MAX": "1",
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
                    resp = client.post("/tasks", json=_test_payload("test-201"))
                    assert resp.status_code == 201

                    # Give it far more time than 1 retry needs; if the
                    # counter bug is present this loops forever and the
                    # task never reaches a terminal state.
                    deadline = time.time() + 5.0
                    status = None
                    while time.time() < deadline:
                        rows = TaskQueue(tmp_db).list_all_with_status()
                        row = next((r for r in rows if r["task_id"] == "test-201"), None)
                        status = row["status"] if row else None
                        if status == "failed":
                            break
                        time.sleep(0.1)

    assert status == "failed", (
        f"task should give up and fail after AGENT_CREW_TRANSIENT_RETRY_MAX=1 "
        f"retries, but ended in status={status!r} (spawn_count={spawn_count})"
    )
    # initial attempt + exactly 1 retry = 2 spawns, capped (not unbounded).
    assert spawn_count == 2, (
        f"expected exactly 2 dispatch attempts (initial + 1 retry), got {spawn_count}"
    )
