import shutil
import subprocess
import time

import pytest

from agent_crew.session import check_health, load_sessions, refresh_pane, save_sessions


pytestmark = pytest.mark.integration

_SESSION = "test_crew_session"

requires_tmux = pytest.mark.skipif(
    not shutil.which("tmux"),
    reason="tmux not available",
)


@pytest.fixture(autouse=True)
def tmux_lifecycle():
    subprocess.run(["tmux", "kill-session", "-t", _SESSION], capture_output=True)
    subprocess.run(["tmux", "new-session", "-d", "-s", _SESSION], capture_output=True)
    yield
    subprocess.run(["tmux", "kill-session", "-t", _SESSION], capture_output=True)


# I-SS01: tmux 세션+pane 생성 → pane target 존재 확인
@requires_tmux
def test_i_ss01_pane_exists():
    result = subprocess.run(
        ["tmux", "list-panes", "-t", _SESSION],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert len(result.stdout.strip()) > 0


# I-SS02: 살아있는 pane에 check_health() → True
@requires_tmux
def test_i_ss02_check_health_alive():
    agent = {"name": "claude", "pane": 0, "cmd": "echo alive", "started_at": time.time(), "failures": 0}
    assert check_health(agent, session=_SESSION) is True


# I-SS03: 실제 pane 생성 후 kill → check_health() → False
@requires_tmux
def test_i_ss03_check_health_dead():
    # window 1 생성 후 살아있음 확인
    subprocess.run(["tmux", "new-window", "-t", _SESSION], capture_output=True)
    agent = {"name": "claude", "pane": 1, "cmd": "echo alive", "started_at": time.time(), "failures": 0}
    assert check_health(agent, session=_SESSION) is True

    # window 1 kill 후 dead 확인
    subprocess.run(["tmux", "kill-window", "-t", f"{_SESSION}:1"], capture_output=True)
    assert check_health(agent, session=_SESSION) is False


# I-SS04: refresh_pane(sessions_path) → sessions.json의 started_at/failures 리셋 확인
@requires_tmux
def test_i_ss04_refresh_pane_resets_session(tmp_path):
    sessions_file = str(tmp_path / "sessions.json")
    old_started_at = time.time() - 7200
    agent = {
        "name": "claude",
        "pane": 0,
        "cmd": "echo refreshed",
        "started_at": old_started_at,
        "failures": 2,
    }
    save_sessions(sessions_file, [agent])

    before = time.time()
    refresh_pane(agent, session=_SESSION, sessions_path=sessions_file)

    reloaded = load_sessions(sessions_file)
    assert reloaded[0]["failures"] == 0
    assert reloaded[0]["started_at"] >= before


# I-SS05: sessions.json round-trip — write → read → 내용 일치
@requires_tmux
def test_i_ss05_sessions_roundtrip(tmp_path):
    sessions_file = str(tmp_path / "sessions.json")
    agents = [
        {"name": "claude", "pane": 0, "cmd": "claude --continue", "started_at": time.time(), "failures": 0},
        {"name": "codex", "pane": 1, "cmd": "codex --resume", "started_at": time.time(), "failures": 1},
    ]
    save_sessions(sessions_file, agents)
    loaded = load_sessions(sessions_file)

    assert len(loaded) == 2
    assert loaded[0]["name"] == "claude"
    assert loaded[1]["name"] == "codex"
    assert loaded[1]["failures"] == 1
    assert loaded[0]["cmd"] == "claude --continue"
