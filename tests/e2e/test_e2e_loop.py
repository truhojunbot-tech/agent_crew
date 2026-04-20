"""
E2E tests for crew run CLI — full code-review loop with real subprocess stub agents.

E-LO01: crew run → stub coder → stub reviewer approves → stub tester passes → done
E-LO02: reviewer requests changes once → 2 implement cycles → approved + tested
E-LO03: crew run --max-iter 2, perpetual rejection → escalation gate after 2 iterations
E-LO04: crew run --no-tester → no test task enqueued after approval
"""

import os
import pathlib
import socket
import subprocess
import sys
import threading
import time

import pytest
import uvicorn
from click.testing import CliRunner

from agent_crew.cli import crew
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app

pytestmark = pytest.mark.e2e

_STUB_SCRIPT = str(pathlib.Path(__file__).parent / "stubs" / "stub_agent.py")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path):
    """Start a real uvicorn server; yield (port, db_path); stop after test."""
    db_path = str(tmp_path / "tasks.db")
    port = _find_free_port()
    app = create_app(db_path)

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server ready
    import httpx
    deadline = time.time() + 10.0
    while time.time() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{port}/tasks", timeout=0.5)
            break
        except Exception:
            time.sleep(0.05)

    yield port, db_path

    server.should_exit = True
    thread.join(timeout=5.0)


def _stub(port: int, role: str, verdict: str | None = None, status: str = "completed", timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Run stub_agent.py as a subprocess and wait for it to finish."""
    env = {
        **os.environ,
        "STUB_PORT": str(port),
        "STUB_ROLE": role,
        "STUB_STATUS": status,
        "STUB_TIMEOUT": str(timeout),
    }
    if verdict:
        env["STUB_VERDICT"] = verdict
    return subprocess.run(
        [sys.executable, _STUB_SCRIPT],
        env=env,
        capture_output=True,
        timeout=timeout + 5,
    )


def _start_cli(args: list[str]):
    """Invoke CLI in a daemon thread. Returns (thread, result_holder)."""
    runner = CliRunner()
    holder: list = [None]

    def _run():
        holder[0] = runner.invoke(crew, args)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t, holder


# E-LO01: basic happy path — coder done, reviewer approves, tester passes
def test_e_lo01_basic_approve(live_server):
    port, db_path = live_server

    cli_t, holder = _start_cli(["run", "feat X", "--db", db_path, "--branch", "main"])

    assert _stub(port, "coder").returncode == 0
    assert _stub(port, "reviewer", verdict="approve").returncode == 0
    assert _stub(port, "tester").returncode == 0

    cli_t.join(timeout=30.0)
    assert not cli_t.is_alive(), "CLI thread timed out"

    r = holder[0]
    assert r.exit_code == 0, r.output
    assert "approved" in r.output.lower()


# E-LO02: reviewer requests changes once → 2 implement cycles → approved and tested
def test_e_lo02_request_changes_then_approve(live_server):
    port, db_path = live_server

    cli_t, holder = _start_cli(["run", "feat Y", "--db", db_path, "--branch", "main"])

    # iteration 1: coder done, reviewer rejects
    assert _stub(port, "coder").returncode == 0
    assert _stub(port, "reviewer", verdict="request_changes").returncode == 0
    # iteration 2: coder done, reviewer approves, tester passes
    assert _stub(port, "coder").returncode == 0
    assert _stub(port, "reviewer", verdict="approve").returncode == 0
    assert _stub(port, "tester").returncode == 0

    cli_t.join(timeout=60.0)
    assert not cli_t.is_alive(), "CLI thread timed out"

    r = holder[0]
    assert r.exit_code == 0, r.output
    assert "approved" in r.output.lower()


# E-LO03: --max-iter 2, perpetual rejection → escalation gate created
def test_e_lo03_escalation_gate(live_server):
    port, db_path = live_server

    cli_t, holder = _start_cli(
        ["run", "feat Z", "--db", db_path, "--branch", "main", "--max-iter", "2"]
    )

    # iteration 1: coder done, reviewer rejects
    assert _stub(port, "coder").returncode == 0
    assert _stub(port, "reviewer", verdict="request_changes").returncode == 0
    # iteration 2: coder done, reviewer rejects again → escalate
    assert _stub(port, "coder").returncode == 0
    assert _stub(port, "reviewer", verdict="request_changes").returncode == 0

    cli_t.join(timeout=60.0)
    assert not cli_t.is_alive(), "CLI thread timed out"

    r = holder[0]
    assert r.exit_code == 0, r.output
    assert "escalated" in r.output.lower()

    queue = TaskQueue(db_path)
    gates = queue.list_gates()
    assert len(gates) >= 1
    assert gates[0].type == "escalation"


# E-LO04: --no-tester → no test task is enqueued after approval
def test_e_lo04_no_tester(live_server):
    port, db_path = live_server

    cli_t, holder = _start_cli(
        ["run", "feat W", "--db", db_path, "--branch", "main", "--no-tester"]
    )

    assert _stub(port, "coder").returncode == 0
    assert _stub(port, "reviewer", verdict="approve").returncode == 0

    cli_t.join(timeout=30.0)
    assert not cli_t.is_alive(), "CLI thread timed out"

    r = holder[0]
    assert r.exit_code == 0, r.output
    assert "no tester" in r.output.lower()

    queue = TaskQueue(db_path)
    all_tasks = queue.list_tasks()
    test_tasks = [t for t in all_tasks if t.task_type == "test"]
    assert len(test_tasks) == 0
