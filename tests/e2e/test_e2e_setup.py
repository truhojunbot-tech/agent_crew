"""
E2E tests for crew setup / status / teardown CLI lifecycle.

Real git repos and tmux sessions are used.
Agent CLIs (claude/codex/gemini) are never started — panes are created
but left at a shell prompt (no command is sent).
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import time

import pytest
from click.testing import CliRunner

from agent_crew.cli import crew


pytestmark = pytest.mark.e2e

requires_tmux = pytest.mark.skipif(
    not shutil.which("tmux"),
    reason="tmux not available",
)


@pytest.fixture
def git_repo(tmp_path):
    """A minimal git repo with one commit."""
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


def _tmux_pane_exists(session: str, pane: int = 0) -> bool:
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", f"{session}:{pane}", "-p"],
        capture_output=True,
    )
    return result.returncode == 0


def _port_listening(port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def _kill_server(state: dict) -> None:
    pid = state.get("server_pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass


@pytest.fixture(autouse=True)
def cleanup_tmux():
    yield
    for name in ("crew_testproj", "crew_myproj"):
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)


def _read_state(base_dir: str, project: str) -> dict:
    return json.loads(open(os.path.join(base_dir, project, "state.json")).read())


# E-ST01: crew setup → worktrees created, panes exist, server running, port file written
@requires_tmux
def test_e_st01_setup_creates_artifacts(monkeypatch, git_repo, base_dir):
    monkeypatch.chdir(git_repo)
    runner = CliRunner()

    result = runner.invoke(crew, ["setup", "testproj", "--agents", "claude", "--base", base_dir])

    assert result.exit_code == 0, result.output
    assert "Setup complete" in result.output

    state = _read_state(base_dir, "testproj")

    # port file written
    port_file = os.path.join(base_dir, "testproj", "port")
    assert os.path.exists(port_file)
    port = int(open(port_file).read().strip())
    assert port > 0

    # worktree created
    wt_path = state["worktrees"]["claude"]
    assert os.path.isdir(wt_path)

    # tmux pane exists
    assert _tmux_pane_exists("crew_testproj", 0)

    # server already confirmed listening by setup command itself
    assert _port_listening(port, timeout=2.0), f"server not listening on {port}"

    _kill_server(state)


# E-ST02: crew status after setup → shows agent alive, port, 0 tasks
@requires_tmux
def test_e_st02_status_after_setup(monkeypatch, git_repo, base_dir):
    monkeypatch.chdir(git_repo)
    runner = CliRunner()

    runner.invoke(crew, ["setup", "testproj", "--agents", "claude", "--base", base_dir])
    state = _read_state(base_dir, "testproj")
    port = state["port"]

    result = runner.invoke(crew, ["status", "testproj", "--base", base_dir])

    assert result.exit_code == 0, result.output
    assert f"Port: {port}" in result.output
    assert "Tasks: 0" in result.output
    assert "claude: alive" in result.output

    _kill_server(state)


# E-ST03: crew teardown → worktrees removed, panes closed, port file deleted
@requires_tmux
def test_e_st03_teardown_cleans_up(monkeypatch, git_repo, base_dir):
    monkeypatch.chdir(git_repo)
    runner = CliRunner()

    runner.invoke(crew, ["setup", "testproj", "--agents", "claude", "--base", base_dir])
    state = _read_state(base_dir, "testproj")
    wt_path = state["worktrees"]["claude"]
    port_file = state["port_file"]

    result = runner.invoke(crew, ["teardown", "testproj", "--base", base_dir])

    assert result.exit_code == 0, result.output
    assert "Teardown complete" in result.output

    # worktree removed
    assert not os.path.isdir(wt_path)

    # port file deleted (entire project dir removed)
    assert not os.path.exists(port_file)

    # tmux pane closed
    assert not _tmux_pane_exists("crew_testproj", 0)

    # state file gone
    assert not os.path.exists(os.path.join(base_dir, "testproj", "state.json"))


# E-ST04: crew setup outside git repo → ClickException: not a git repository
def test_e_st04_setup_outside_git_repo(monkeypatch, tmp_path, base_dir):
    non_git = tmp_path / "notgit"
    non_git.mkdir()
    monkeypatch.chdir(non_git)
    runner = CliRunner()

    result = runner.invoke(crew, ["setup", "testproj", "--base", base_dir])

    assert result.exit_code != 0
    assert "not a git repository" in result.output


# E-ST05: crew setup --agents claude → only claude worktree/pane created
@requires_tmux
def test_e_st05_custom_agents(monkeypatch, git_repo, base_dir):
    monkeypatch.chdir(git_repo)
    runner = CliRunner()

    result = runner.invoke(crew, ["setup", "testproj", "--agents", "claude", "--base", base_dir])

    assert result.exit_code == 0, result.output

    state = _read_state(base_dir, "testproj")
    assert state["agents"] == ["claude"]
    assert "claude" in state["worktrees"]
    assert "codex" not in state["worktrees"]
    assert "gemini" not in state["worktrees"]
    assert _tmux_pane_exists("crew_testproj", 0)
    assert not _tmux_pane_exists("crew_testproj", 1)

    _kill_server(state)


# E-ST06: double crew setup same project → second invocation errors out
@requires_tmux
def test_e_st06_double_setup_errors(monkeypatch, git_repo, base_dir):
    monkeypatch.chdir(git_repo)
    runner = CliRunner()

    first = runner.invoke(crew, ["setup", "testproj", "--agents", "claude", "--base", base_dir])
    assert first.exit_code == 0, first.output

    second = runner.invoke(crew, ["setup", "testproj", "--agents", "claude", "--base", base_dir])
    assert second.exit_code != 0
    assert "already set up" in second.output

    state = _read_state(base_dir, "testproj")
    _kill_server(state)
