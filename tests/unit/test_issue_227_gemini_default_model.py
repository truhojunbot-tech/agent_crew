"""Regression tests for #227 and #776 Gemini model resolution at dispatch."""
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
        "description": "run the suite",
        "branch": "main",
        "priority": 3,
        "context": {},
        "project": "test_project",
    }


def _setup(tmp_path):
    wt_claude = tmp_path / "claude"
    wt_codex = tmp_path / "codex"
    wt_gemini = tmp_path / "gemini"
    for wt in (wt_claude, wt_codex, wt_gemini):
        wt.mkdir()
        (wt / ".git").mkdir()

    state = {
        "role_agents": {"implementer": "codex", "reviewer": "claude", "tester": "gemini"}, "worktrees": {
            "claude": str(wt_claude),
            "codex": str(wt_codex),
            "gemini": str(wt_gemini),
        }
    }
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state))
    return state_file


def _run_dispatch(tmp_db, tmp_path, task_id, extra_env=None, *, unused_tcp_port):
    state_file = _setup(tmp_path)
    spawn_log: list[list[str]] = []

    async def fake_subprocess(*args, **_kwargs):
        spawn_log.append([str(a) for a in args])
        proc = MagicMock()
        proc.returncode = 0
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        return proc

    env = {
        "AGENT_CREW_DISPATCHER": "1",
        "AGENT_CREW_DISPATCH_INTERVAL": "0.05",
        "AGENT_CREW_WORKTREE_SYNC_DISABLED": "1",
    }
    if extra_env:
        env.update(extra_env)

    with patch.dict(os.environ, env):
        if "AGENT_CREW_GEMINI_MODEL" not in env:
            # A host/CI override must not be able to mask a reverted
            # default (review-45d57692) -- ensure the default-path test
            # actually exercises the default, not whatever happens to be
            # set outside this test. patch.dict restores the real
            # environ on exit, so popping here is safe.
            os.environ.pop("AGENT_CREW_GEMINI_MODEL", None)
        with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
            app = create_app(
                db_path=tmp_db,
                pane_map={},
                port=unused_tcp_port,
                state_path=str(state_file),
                watchdog_disabled=True,
                anomaly_disabled=True,
                identity_required=False,
                coordinator_notify_fn=lambda message: True,
            )
            with TestClient(app) as client:
                resp = client.post("/tasks", json=_test_payload(task_id))
                assert resp.status_code == 201
                queue = TaskQueue(tmp_db)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    status = queue.get_task_status(task_id)
                    if spawn_log or status in {"needs_human", "failed", "timed_out", "completed"}:
                        break
                    time.sleep(0.02)
                else:
                    raise AssertionError(f"test task {task_id} was not dispatched or stopped")

    return spawn_log


def _model_arg(cmd: list[str]) -> str:
    assert "--model" in cmd, f"gemini dispatch missing --model flag: {cmd}"
    return cmd[cmd.index("--model") + 1]


def test_u227_default_gemini_model_is_a_currently_valid_name(tmp_db, tmp_path, *, unused_tcp_port):
    """Without AGENT_CREW_GEMINI_MODEL, the dispatched --model must NOT be
    the retired "Gemini 3.5 Flash (Medium)" name that broke every gemini
    dispatch, and must be the current successor."""
    spawn_log = _run_dispatch(tmp_db, tmp_path, "test-227-a", unused_tcp_port=unused_tcp_port)

    assert spawn_log, "test task was never dispatched"
    model = _model_arg(spawn_log[0])
    assert model != "Gemini 3.5 Flash (Medium)", (
        "default gemini model regressed back to the retired name that agy "
        "rejects with 'model ... is not recognized'"
    )
    assert model == "Gemini 3.8 Flash (Medium)"


def test_u227_env_override_still_honored(tmp_db, tmp_path, *, unused_tcp_port):
    """AGENT_CREW_GEMINI_MODEL must still take precedence over the default,
    so operators can pin a specific model without a code change."""
    spawn_log = _run_dispatch(
        tmp_db, tmp_path, "test-227-b",
        extra_env={"AGENT_CREW_GEMINI_MODEL": "Gemini 3.8 Flash (High)"},
        unused_tcp_port=unused_tcp_port,
    )

    assert spawn_log, "test task was never dispatched"
    model = _model_arg(spawn_log[0])
    assert model == "Gemini 3.8 Flash (High)"


def test_u776_explicit_model_pin_is_recorded_without_substitution(
        tmp_db, tmp_path, *, unused_tcp_port):
    from agent_crew.queue import TaskQueue
    spawn_log = _run_dispatch(
        tmp_db, tmp_path, "test-776-pin",
        extra_env={"AGENT_CREW_GEMINI_MODEL": "Gemini 3.7 Flash (Medium)"},
        unused_tcp_port=unused_tcp_port)
    assert _model_arg(spawn_log[0]) == "Gemini 3.7 Flash (Medium)"
    assert TaskQueue(tmp_db).get_attribution("test-776-pin")["model"] == "Gemini 3.7 Flash (Medium)"
