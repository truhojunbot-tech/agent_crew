"""
E2E tests for crew discuss CLI — discussion loop with subprocess stub agents.

E-DI01: crew discuss "topic" — all stub agents respond, synthesis.md written
E-DI02: crew discuss --rounds 2 — two rounds, round 2 context references round 1
E-DI03: crew discuss --then-run — synthesis triggers code-review loop (implement task enqueued)
"""

import json
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
from agent_crew.server import create_app
from agent_crew.queue import TaskQueue

pytestmark = pytest.mark.e2e

_STUB_SCRIPT = str(pathlib.Path(__file__).parent / "stubs" / "stub_agent.py")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path):
    """Start a real uvicorn server; yield (port, db_path, tmp_path); stop after test."""
    db_path = str(tmp_path / "tasks.db")
    port = _find_free_port()
    app = create_app(db_path)

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    import httpx
    deadline = time.time() + 10.0
    while time.time() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{port}/tasks", timeout=0.5)
            break
        except Exception:
            time.sleep(0.05)

    yield port, db_path, tmp_path

    server.should_exit = True
    thread.join(timeout=5.0)


def _stub(port: int, role: str = "panel", verdict: str | None = None,
          status: str = "completed", poll_timeout: float = 30.0) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "STUB_PORT": str(port),
        "STUB_ROLE": role,
        "STUB_STATUS": status,
        "STUB_TIMEOUT": str(poll_timeout),
    }
    if verdict:
        env["STUB_VERDICT"] = verdict
    return subprocess.run(
        [sys.executable, _STUB_SCRIPT],
        env=env,
        capture_output=True,
        timeout=poll_timeout + 5,
    )


def _start_cli(args: list[str]):
    runner = CliRunner()
    holder: list = [None]

    def _run():
        holder[0] = runner.invoke(crew, args)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t, holder


# E-DI01: basic discussion — 2 agents respond, synthesis.md written with correct structure
def test_e_di01_basic_discussion(live_server, tmp_path):
    port, db_path, _ = live_server
    output = str(tmp_path / "synthesis.md")

    cli_t, holder = _start_cli([
        "discuss", "Should we adopt microservices?",
        "--db", db_path,
        "--agents", "analyst,critic",
        "--output", output,
    ])

    # Two agents → two panel tasks
    assert _stub(port, "panel").returncode == 0
    assert _stub(port, "panel").returncode == 0

    cli_t.join(timeout=30.0)
    assert not cli_t.is_alive(), "CLI thread timed out"

    r = holder[0]
    assert r.exit_code == 0, r.output
    assert "synthesis written" in r.output.lower()

    content = pathlib.Path(output).read_text()
    assert "## Topic" in content
    assert "## Panel Opinions" in content
    assert "## Synthesis" in content
    assert "## Decision" in content
    assert "Should we adopt microservices?" in content


# E-DI02: two rounds — round 2 task context includes prior_synthesis from round 1
def test_e_di02_two_rounds(live_server, tmp_path):
    port, db_path, _ = live_server
    output = str(tmp_path / "synthesis.md")

    cli_t, holder = _start_cli([
        "discuss", "Build vs buy?",
        "--db", db_path,
        "--agents", "analyst,critic",
        "--rounds", "2",
        "--output", output,
    ])

    # Round 1: 2 agents
    assert _stub(port, "panel").returncode == 0
    assert _stub(port, "panel").returncode == 0
    # Round 2: 2 agents
    assert _stub(port, "panel").returncode == 0
    assert _stub(port, "panel").returncode == 0

    cli_t.join(timeout=60.0)
    assert not cli_t.is_alive(), "CLI thread timed out"

    r = holder[0]
    assert r.exit_code == 0, r.output

    # synthesis.md written
    content = pathlib.Path(output).read_text()
    assert "## Topic" in content
    assert "Build vs buy?" in content

    # Round 2 tasks should carry prior_synthesis in context
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT context FROM tasks WHERE task_type = 'discuss' ORDER BY created_at ASC"
    ).fetchall()
    conn.close()

    assert len(rows) == 4  # 2 agents × 2 rounds
    round2_contexts = [json.loads(rows[i]["context"]) for i in (2, 3)]
    assert all("prior_synthesis" in ctx for ctx in round2_contexts)


# E-DI03: --then-run triggers code-review loop (implement task enqueued after synthesis)
def test_e_di03_then_run(live_server, tmp_path):
    port, db_path, _ = live_server
    output = str(tmp_path / "synthesis.md")

    cli_t, holder = _start_cli([
        "discuss", "Adopt event sourcing",
        "--db", db_path,
        "--agents", "analyst",
        "--output", output,
        "--then-run",
    ])

    # One panel agent responds
    assert _stub(port, "panel").returncode == 0

    cli_t.join(timeout=30.0)
    assert not cli_t.is_alive(), "CLI thread timed out"

    r = holder[0]
    assert r.exit_code == 0, r.output
    assert "code-review loop started" in r.output.lower()

    # synthesis.md written
    assert pathlib.Path(output).exists()

    # implement task enqueued in DB
    queue = TaskQueue(db_path)
    all_tasks = queue.list_tasks()
    impl_tasks = [t for t in all_tasks if t.task_type == "implement"]
    assert len(impl_tasks) == 1
    assert "adopt event sourcing" in impl_tasks[0].description.lower()
