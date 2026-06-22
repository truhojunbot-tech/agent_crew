"""Regression test for issue #188: --reviewer agent_override ignored.

Bug: ``crew run --reviewer gemini`` set ``task.context["agent_override"]="gemini"``,
but the headless dispatcher routed by role alone (codex for reviewer), so every
review went to codex regardless of the flag — a SPOF whenever codex hit quota.

Fix: ``_dispatch_task`` honors ``task.context["agent_override"]`` and redirects
both the agent command and the worktree to the override agent's worktree.
"""
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from agent_crew.server import create_app


def _review_payload(task_id: str, override_agent: str) -> dict:
    return {
        "task_id": task_id,
        "task_type": "review",
        "description": "review PR #1",
        "branch": "main",
        "priority": 3,
        "context": {"agent_override": override_agent},
        "project": "test_project",
    }


def test_u_b188_reviewer_override_routes_to_override_agent(tmp_db, tmp_path):
    """--reviewer gemini must spawn `gemini`, not `codex`, in gemini's worktree."""
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

    spawn_log: list[tuple[str, str]] = []  # (agent, cwd)

    async def fake_subprocess(*args, **kwargs):
        cmd0 = str(args[0]) if args else ""
        cwd = kwargs.get("cwd", "")
        # gemini role now dispatches via the `agy` binary (Antigravity CLI)
        # after Google retired gemini-cli for oauth-personal on 2026-06-18.
        if "gemini" in cmd0 or cmd0.endswith("/agy") or cmd0 == "agy":
            spawn_log.append(("gemini", cwd))
        elif "codex" in cmd0:
            spawn_log.append(("codex", cwd))
        else:
            spawn_log.append(("claude", cwd))
        proc = MagicMock()
        proc.returncode = 0
        proc.kill = MagicMock()
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
                    resp = client.post("/tasks", json=_review_payload("review-1", "gemini"))
                    assert resp.status_code == 201
                    time.sleep(0.4)

    assert spawn_log, "review task was never dispatched"
    spawned_agent, spawned_cwd = spawn_log[0]
    assert spawned_agent == "gemini", (
        f"--reviewer gemini should route to gemini, got {spawned_agent} (log={spawn_log})"
    )
    assert spawned_cwd == str(wt_gemini), (
        f"--reviewer gemini should run in gemini worktree, got cwd={spawned_cwd}"
    )


def test_u_b188_no_override_keeps_role_default(tmp_db, tmp_path):
    """Without agent_override the dispatcher still uses the role default (codex)."""
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

    spawn_log: list[tuple[str, str]] = []

    async def fake_subprocess(*args, **kwargs):
        cmd0 = str(args[0]) if args else ""
        cwd = kwargs.get("cwd", "")
        # gemini role now dispatches via the `agy` binary (Antigravity CLI)
        # after Google retired gemini-cli for oauth-personal on 2026-06-18.
        if "gemini" in cmd0 or cmd0.endswith("/agy") or cmd0 == "agy":
            spawn_log.append(("gemini", cwd))
        elif "codex" in cmd0:
            spawn_log.append(("codex", cwd))
        else:
            spawn_log.append(("claude", cwd))
        proc = MagicMock()
        proc.returncode = 0
        proc.kill = MagicMock()
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
                    payload = _review_payload("review-2", "")
                    payload["context"] = {}  # no agent_override
                    resp = client.post("/tasks", json=payload)
                    assert resp.status_code == 201
                    time.sleep(0.4)

    assert spawn_log, "review task was never dispatched"
    spawned_agent, spawned_cwd = spawn_log[0]
    assert spawned_agent == "codex", (
        f"without override the reviewer role should run codex, got {spawned_agent}"
    )
    assert spawned_cwd == str(wt_codex), (
        f"without override the reviewer should run in codex worktree, got cwd={spawned_cwd}"
    )
