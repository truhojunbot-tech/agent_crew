"""Regression test for issue #227: gemini tester default model no longer
exists.

Bug: `_dispatch_task` hardcoded the gemini `--model` default to
"Gemini 3.5 Flash (Medium)" (used whenever AGENT_CREW_GEMINI_MODEL is
unset). agy retired that model — every gemini dispatch on the default
config failed immediately and non-retriably with "model ... is not
recognized as a known model or custom model in settings", 100% failure
rate on every project that hadn't set the env override.

Fix: default updated to "Gemini 3.7 Flash (Medium)", the direct
successor. This test asserts the constructed `agy ... --model <value>`
command uses that string by default and still honors
AGENT_CREW_GEMINI_MODEL when set, so a future silent drift like this
shows up in a diff/review instead of only being discovered live.
"""
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

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
        "worktrees": {
            "claude": str(wt_claude),
            "codex": str(wt_codex),
            "gemini": str(wt_gemini),
        }
    }
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state))
    return state_file


def _run_dispatch(tmp_db, tmp_path, task_id, extra_env=None):
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
                    resp = client.post("/tasks", json=_test_payload(task_id))
                    assert resp.status_code == 201
                    time.sleep(0.4)

    return spawn_log


def _model_arg(cmd: list[str]) -> str:
    assert "--model" in cmd, f"gemini dispatch missing --model flag: {cmd}"
    return cmd[cmd.index("--model") + 1]


def test_u227_default_gemini_model_is_a_currently_valid_name(tmp_db, tmp_path):
    """Without AGENT_CREW_GEMINI_MODEL, the dispatched --model must NOT be
    the retired "Gemini 3.5 Flash (Medium)" name that broke every gemini
    dispatch, and must be the current successor."""
    spawn_log = _run_dispatch(tmp_db, tmp_path, "test-227-a")

    assert spawn_log, "test task was never dispatched"
    model = _model_arg(spawn_log[0])
    assert model != "Gemini 3.5 Flash (Medium)", (
        "default gemini model regressed back to the retired name that agy "
        "rejects with 'model ... is not recognized'"
    )
    assert model == "Gemini 3.7 Flash (Medium)"


def test_u227_env_override_still_honored(tmp_db, tmp_path):
    """AGENT_CREW_GEMINI_MODEL must still take precedence over the default,
    so operators can pin a specific model without a code change."""
    spawn_log = _run_dispatch(
        tmp_db, tmp_path, "test-227-b",
        extra_env={"AGENT_CREW_GEMINI_MODEL": "Gemini 3.7 Flash (High)"},
    )

    assert spawn_log, "test task was never dispatched"
    model = _model_arg(spawn_log[0])
    assert model == "Gemini 3.7 Flash (High)"
