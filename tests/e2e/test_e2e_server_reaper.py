"""
#879: E2E tests must never leak a crew server, nor open one in 8100-8199.

E-RP01: setup + recover inside a reaper, then a forced assertion failure →
        every recorded server pid is dead and no port was in the crew range
E-RP02: terminate_pids escalates to SIGKILL for a process ignoring SIGTERM
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

import pytest
from click.testing import CliRunner

from agent_crew.cli import crew
from tests.e2e.conftest import CREW_PORT_RANGE, CrewServerReaper, _pid_alive, terminate_pids

pytestmark = pytest.mark.e2e


def _port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


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


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not available")
def test_e_rp01_failed_test_leaks_no_server(monkeypatch, git_repo, base_dir, e2e_project, tmp_path):  # noqa: ARG001 — tmux isolation
    monkeypatch.chdir(git_repo)
    runner = CliRunner()
    setup_pid = recover_pid = None
    state_path = os.path.join(base_dir, "rpproj", "state.json")

    with pytest.MonkeyPatch.context() as mp:
        reaper = CrewServerReaper(mp, tmp_path)
        with pytest.raises(AssertionError, match="forced"):
            with reaper:
                result = runner.invoke(crew, ["setup", "rpproj", "--agents", "claude",
                                              "--base", base_dir])
                assert result.exit_code == 0, result.output
                setup_pid = json.loads(open(state_path).read())["server_pid"]

                # Leave the setup server running and record a second one from
                # recover: the old test pattern only ever saw the last pid.
                # Reap it too: setup's Popen lives in this process, and recover
                # treats a zombie as a live server.
                os.kill(setup_pid, signal.SIGKILL)
                os.waitpid(setup_pid, 0)
                result = runner.invoke(crew, ["recover", "rpproj", "--base", base_dir])
                assert result.exit_code == 0, result.output
                recover_pid = json.loads(open(state_path).read())["server_pid"]
                assert recover_pid != setup_pid
                assert _pid_alive(recover_pid)
                raise AssertionError("forced failure after setup+recover")

    assert {setup_pid, recover_pid} <= reaper.pids
    assert reaper.ports, "allocator wrapper was not used"
    for pid in reaper.all_pids():
        assert not _pid_alive(pid), f"server pid {pid} survived a failing test"
    for port in reaper.ports:
        assert port not in CREW_PORT_RANGE
        assert not _port_listening(port)


def test_e_rp02_terminate_escalates_to_sigkill():
    proc = subprocess.Popen([sys.executable, "-c",
                             "import signal, time\n"
                             "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                             "print('ready', flush=True)\n"
                             "time.sleep(60)\n"],
                            stdout=subprocess.PIPE)
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == b"ready"
    started = time.time()
    terminate_pids([proc.pid], grace=0.5)
    assert proc.wait(timeout=5) == -signal.SIGKILL
    assert time.time() - started < 5
