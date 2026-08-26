"""
E2E tests for crew setup / status / teardown CLI lifecycle.

Real git repos and tmux sessions are used, but every test runs its own
`crew setup` inside a disposable tmux session (see `e2e_project` /
`isolated_tmux_session` in conftest.py, #207) — never the session the test
process itself happens to be running in.

Agent CLIs (claude/codex/gemini) are never started — panes are created
but left at a shell prompt (no command is sent).
"""

import json
import os
import shutil
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


def _tmux_pane_exists(target: str) -> bool:
    """`target` may be a raw pane_id (e.g. '%246') or a 'session:window.pane'
    spec. Using pane_ids from state['pane_ids'] rather than a guessed index
    is what actually identifies *the agent's* pane — dispatcher mode
    split-windows a new pane for every agent (never commandeers the
    session's original pane 0), so index 0 in a freshly created session is
    the leftover shell from `tmux new-session`, not an agent pane."""
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", target, "-p"],
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


def _read_state(base_dir: str, project: str) -> dict:
    return json.loads(open(os.path.join(base_dir, project, "state.json")).read())


# E-ST01: crew setup → worktrees created, panes exist, server running, port file written
@requires_tmux
def test_e_st01_setup_creates_artifacts(monkeypatch, git_repo, base_dir, e2e_project, isolated_tmux_session):
    monkeypatch.chdir(git_repo)
    runner = CliRunner()

    result = runner.invoke(crew, ["setup", "testproj", "--agents", "claude", "--base", base_dir])
    e2e_project(base_dir, "testproj")

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

    # pane exists — in the isolated session, not wherever this test happened to run
    assert state["session"] == isolated_tmux_session
    assert len(state["pane_ids"]) == 1
    assert _tmux_pane_exists(state["pane_ids"][0])

    # server already confirmed listening by setup command itself
    assert _port_listening(port, timeout=2.0), f"server not listening on {port}"


# E-ST02: crew status after setup → shows agent alive, port, 0 tasks
@requires_tmux
def test_e_st02_status_after_setup(monkeypatch, git_repo, base_dir, e2e_project):
    monkeypatch.chdir(git_repo)
    runner = CliRunner()

    runner.invoke(crew, ["setup", "testproj", "--agents", "claude", "--base", base_dir])
    e2e_project(base_dir, "testproj")
    state = _read_state(base_dir, "testproj")
    port = state["port"]

    result = runner.invoke(crew, ["status", "testproj", "--base", base_dir])

    assert result.exit_code == 0, result.output
    assert f"Port: {port}" in result.output
    assert "Tasks: 0" in result.output
    # Format is "claude (%246): alive" — the pane id is included.
    assert "claude (" in result.output
    assert "): alive" in result.output


# E-ST03: crew teardown → worktrees removed, panes closed, port file deleted
@requires_tmux
def test_e_st03_teardown_cleans_up(monkeypatch, git_repo, base_dir, e2e_project):
    monkeypatch.chdir(git_repo)
    runner = CliRunner()

    runner.invoke(crew, ["setup", "testproj", "--agents", "claude", "--base", base_dir])
    e2e_project(base_dir, "testproj")
    state = _read_state(base_dir, "testproj")
    wt_path = state["worktrees"]["claude"]
    port_file = state["port_file"]
    agent_pane_id = state["pane_ids"][0]

    result = runner.invoke(crew, ["teardown", "testproj", "--base", base_dir])

    assert result.exit_code == 0, result.output
    assert "Teardown complete" in result.output

    # worktree removed
    assert not os.path.isdir(wt_path)

    # port file deleted (entire project dir removed)
    assert not os.path.exists(port_file)

    # agent's pane closed (the session's original pane 0 from
    # isolated_tmux_session is untouched — teardown only closes pane_ids)
    assert not _tmux_pane_exists(agent_pane_id)

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
def test_e_st05_custom_agents(monkeypatch, git_repo, base_dir, e2e_project, isolated_tmux_session):
    monkeypatch.chdir(git_repo)
    runner = CliRunner()

    result = runner.invoke(crew, ["setup", "testproj", "--agents", "claude", "--base", base_dir])
    e2e_project(base_dir, "testproj")

    assert result.exit_code == 0, result.output

    state = _read_state(base_dir, "testproj")
    assert state["agents"] == ["claude"]
    assert "claude" in state["worktrees"]
    assert "codex" not in state["worktrees"]
    assert "gemini" not in state["worktrees"]
    assert state["session"] == isolated_tmux_session
    assert len(state["pane_ids"]) == 1
    assert _tmux_pane_exists(state["pane_ids"][0])


# E-ST06: double crew setup same project → second invocation is a no-op reuse,
# not an error (setup() returns early with "already set up ... Reusing.").
@requires_tmux
def test_e_st06_double_setup_errors(monkeypatch, git_repo, base_dir, e2e_project):
    monkeypatch.chdir(git_repo)
    runner = CliRunner()

    first = runner.invoke(crew, ["setup", "testproj", "--agents", "claude", "--base", base_dir])
    e2e_project(base_dir, "testproj")
    assert first.exit_code == 0, first.output

    second = runner.invoke(crew, ["setup", "testproj", "--agents", "claude", "--base", base_dir])
    assert second.exit_code == 0, second.output
    assert "already set up" in second.output


# E-ST07 (#210 review): a `crew setup` that spawns its server and writes
# state.json but is never explicitly registered with e2e_project (e.g. the
# test's own assertions raised before it got the chance) must still have
# its server pid reaped — teardown auto-discovers every state.json under
# base_dir, register() calls or not.
def test_e_st07_cleanup_reaps_unregistered_server(base_dir):
    proj_dir = os.path.join(base_dir, "crashedproj")
    os.makedirs(proj_dir, exist_ok=True)

    dummy = subprocess.Popen(["sleep", "300"])
    try:
        assert dummy.poll() is None, "dummy process should start alive"

        state = {
            "project": "crashedproj",
            "port": 0,
            "server_pid": dummy.pid,
            "db": os.path.join(proj_dir, "tasks.db"),
            "session": "",
            "agents": [],
            "worktrees": {},
        }
        with open(os.path.join(proj_dir, "state.json"), "w") as f:
            json.dump(state, f)

        from tests.e2e.conftest import _cleanup_project_dir

        # No register() call — this is exactly the gap the review flagged.
        _cleanup_project_dir(proj_dir)

        deadline = time.time() + 5.0
        while time.time() < deadline and dummy.poll() is None:
            time.sleep(0.1)
        assert dummy.poll() is not None, "server pid should be reaped even without register()"
    finally:
        if dummy.poll() is None:
            dummy.kill()
        dummy.wait(timeout=5)
