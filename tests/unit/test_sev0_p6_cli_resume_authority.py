"""Project resume must use the same signed-snapshot P6 authority as the server."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_crew import provenance
from agent_crew.queue import RefuseAllLoosening, TaskQueue
from tests.unit.test_sev0_cea_s4e_wiring import DECISION_ID, write_key, write_snapshot


@pytest.mark.parametrize(
    ("source", "decision_id", "snapshot_configured", "allowed"),
    [
        ("hojun", DECISION_ID, True, True),
        ("hojun", "T0-UNKNOWN", True, False),
        ("alfred", DECISION_ID, True, False),
        ("hojun", DECISION_ID, False, False),
    ],
)
def test_project_resume_checks_signed_t0_in_fresh_cli_process(
    tmp_path, source, decision_id, snapshot_configured, allowed,
):
    project = "agent_crew"
    state_dir = tmp_path / project
    state_dir.mkdir()
    db_path = state_dir / "tasks.db"
    queue = TaskQueue(str(db_path), runtime_authority=RefuseAllLoosening())
    stopped_epoch = queue.set_stop_epoch(True, incident="test-stop")
    assert queue.get_runtime_state()["state"] == "STOPPED"

    home = tmp_path / "home"
    tmux = tmp_path / "tmux"
    home.mkdir()
    tmux.mkdir()
    env = os.environ.copy()
    # The fresh CLI process must execute this checkout, not an unrelated
    # editable/site install inherited from the host running pytest.
    src = str(Path(__file__).resolve().parents[2] / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (src, env.get("PYTHONPATH", ""))))
    for key in ("AGENT_CREW_CEA_SNAPSHOT_PATH", "AGENT_CREW_CEA_POLICY_SNAPSHOT",
                "AGENT_CREW_CEA_SNAPSHOT_KEY_FILE"):
        env.pop(key, None)
    env.update(HOME=str(home), TMUX_TMPDIR=str(tmux))
    if snapshot_configured:
        running_build = provenance.build()["commit"]
        assert running_build
        env["AGENT_CREW_CEA_SNAPSHOT_PATH"] = str(
            write_snapshot(tmp_path, build_commits=(running_build,)))
        env["AGENT_CREW_CEA_SNAPSHOT_KEY_FILE"] = str(write_key(tmp_path))

    command = [sys.executable, "-c", "from agent_crew.cli import crew; crew()",
               "resume", project, "--base", str(tmp_path),
               "--generation", str(stopped_epoch + 1), "--source", source,
               "--decision-id", decision_id]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    payload = json.loads(result.stdout)
    assert payload["resumed"] is allowed, result.stdout + result.stderr
    assert result.returncode == (0 if allowed else 1), result.stdout + result.stderr
    assert TaskQueue(str(db_path)).get_runtime_state()["state"] == (
        "ACTIVE" if allowed else "STOPPED")
