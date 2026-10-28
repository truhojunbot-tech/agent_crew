"""#874: the suite never reaches the live coordinator notice sink."""

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from agent_crew import server
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue

_INNER_FLAG = "AGENT_CREW_874_INNER"
_REPO = Path(__file__).resolve().parents[2]


def test_fixture_strips_real_delivery_routes():
    assert "AGENT_CREW_COORDINATOR_NOTICE_CMD" not in os.environ
    assert "TELEGRAM_CHAT_ID" not in os.environ
    assert server.coordinator_notice_sink() == "none"


@pytest.mark.skipif(not os.getenv(_INNER_FLAG), reason="driven by the outer test")
def test_inner_triggers_coordinator_notice(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("gate-t", "implement", "fixture", project="agent_crew"))
    server.coordinator_notice_once(queue, "gate-t", "no_result_submitted")


def test_outer_notice_cmd_does_not_reach_sink(tmp_path):
    marker = tmp_path / "posted"
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    env.update({
        "AGENT_CREW_COORDINATOR_NOTICE_CMD":
            f"sh -c {shlex.quote(f'cat > {marker}')}",
        _INNER_FLAG: "1",
        # conftest moves HOME, so hand the child our import path explicitly.
        "PYTHONPATH": os.pathsep.join([str(_REPO / "src"), *sys.path]),
    })
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"{__file__}::test_inner_triggers_coordinator_notice"],
        cwd=_REPO, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert not marker.exists()
