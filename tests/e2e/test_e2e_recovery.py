"""
E2E tests for crew recover CLI — restore after tmux/server crash.

E-RC01: Kill tmux session → crew recover → tmux session recreated
E-RC02: Kill server process → crew recover → server restarted, port listening again
E-RC03: Enqueue task → kill server → recover → task still pending (SQLite persistence)
E-RC04: crew recover with no prior setup → ClickException: not found
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time

import pytest
import uvicorn
from click.testing import CliRunner

from agent_crew.cli import crew
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app

pytestmark = pytest.mark.e2e

requires_tmux = pytest.mark.skipif(
    not shutil.which("tmux"),
    reason="tmux not available",
)


# ── shared helpers ────────────────────────────────────────────────────────────

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_listening(port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def _tmux_session_exists(session: str) -> bool:
    return subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True,
    ).returncode == 0


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in [
        ["git", "init", str(repo)],
        ["git", "-C", str(repo), "config", "user.email", "t@t.com"],
        ["git", "-C", str(repo), "config", "user.name", "T"],
    ]:
        subprocess.run(cmd, capture_output=True)
    (repo / "README.md").write_text("test")
    subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], capture_output=True)
    return repo


@pytest.fixture
def base_dir(tmp_path):
    d = tmp_path / "base"
    d.mkdir()
    return str(d)


def _read_state(base: str, project: str) -> dict:
    return json.loads(open(os.path.join(base, project, "state.json")).read())


def _kill_server(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass


@pytest.fixture(autouse=True)
def cleanup_tmux():
    yield
    for name in ("crew_rcproj",):
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)


# ── E-RC01: Kill tmux → recover → session recreated ──────────────────────────

@requires_tmux
def test_e_rc01_recover_tmux(monkeypatch, git_repo, base_dir):
    monkeypatch.chdir(git_repo)
    runner = CliRunner()

    result = runner.invoke(crew, ["setup", "rcproj", "--agents", "claude", "--base", base_dir])
    assert result.exit_code == 0, result.output

    state = _read_state(base_dir, "rcproj")
    session_name = state["session"]

    assert _tmux_session_exists(session_name)
    subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)
    assert not _tmux_session_exists(session_name)

    result = runner.invoke(crew, ["recover", "rcproj", "--base", base_dir])
    assert result.exit_code == 0, result.output
    assert "tmux" in result.output.lower()

    assert _tmux_session_exists(session_name)

    # cleanup server
    state2 = _read_state(base_dir, "rcproj")
    _kill_server(state2["server_pid"])


# ── E-RC02: Kill server → recover → server listening again ───────────────────

@requires_tmux
def test_e_rc02_recover_server(monkeypatch, git_repo, base_dir):
    monkeypatch.chdir(git_repo)
    runner = CliRunner()

    result = runner.invoke(crew, ["setup", "rcproj", "--agents", "claude", "--base", base_dir])
    assert result.exit_code == 0, result.output

    state = _read_state(base_dir, "rcproj")
    port = state["port"]
    assert _port_listening(port, timeout=2.0)

    _kill_server(state["server_pid"])
    # wait for port to close
    deadline = time.time() + 10.0
    while time.time() < deadline and _port_listening(port, timeout=0.2):
        time.sleep(0.1)

    assert not _port_listening(port, timeout=1.0)

    result = runner.invoke(crew, ["recover", "rcproj", "--base", base_dir])
    assert result.exit_code == 0, result.output
    assert "server" in result.output.lower()

    assert _port_listening(port, timeout=10.0)

    state2 = _read_state(base_dir, "rcproj")
    _kill_server(state2["server_pid"])


# ── E-RC03: SQLite persistence — task survives server kill ───────────────────

def test_e_rc03_sqlite_persistence(tmp_path):
    port = _find_free_port()
    db_path = str(tmp_path / "tasks.db")

    base = str(tmp_path / "base")
    proj_state_dir = os.path.join(base, "persistproj")
    os.makedirs(proj_state_dir, exist_ok=True)

    port_file = os.path.join(proj_state_dir, "port")
    with open(port_file, "w") as f:
        f.write(str(port))

    # Start server in a thread
    app = create_app(db_path)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    assert _port_listening(port, timeout=10.0)

    # Enqueue a task via HTTP
    import httpx
    resp = httpx.post(f"http://127.0.0.1:{port}/tasks", json={
        "task_id": "persist-task-001",
        "task_type": "implement",
        "description": "Persistent task",
        "branch": "main",
    }, timeout=5.0)
    assert resp.status_code == 201

    # Kill server
    server.should_exit = True
    t.join(timeout=10.0)
    assert not _port_listening(port, timeout=2.0)

    # Write minimal state for recover
    state = {
        "project": "persistproj",
        "port": port,
        "port_file": port_file,
        "session": "crew_persistproj",
        "agents": [],
        "worktrees": {},
        "db": db_path,
        "server_pid": 0,
        "sessions_file": os.path.join(proj_state_dir, "sessions.json"),
    }
    with open(os.path.join(proj_state_dir, "state.json"), "w") as f:
        json.dump(state, f)

    runner = CliRunner()
    result = runner.invoke(crew, ["recover", "persistproj", "--base", base])
    assert result.exit_code == 0, result.output
    assert "server" in result.output.lower()
    assert _port_listening(port, timeout=10.0)

    # Task still in DB
    queue = TaskQueue(db_path)
    tasks = queue.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].task_id == "persist-task-001"

    # cleanup restarted server
    state2 = json.loads(open(os.path.join(proj_state_dir, "state.json")).read())
    _kill_server(state2.get("server_pid", 0))


# ── E-RC04: no prior setup → error ───────────────────────────────────────────

def test_e_rc04_no_setup_error(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        crew,
        ["recover", "ghost_project", "--base", str(tmp_path / "no_base")],
    )
    assert result.exit_code != 0
    assert "not found" in result.output.lower()
