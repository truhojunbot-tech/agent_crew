import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

import pytest

from agent_crew.session import (
    SESSION_MAX_FAILURES,
    SESSION_MAX_HOURS,
    check_health,
    increment_failure,
    load_sessions,
    refresh_needed,
    refresh_pane,
    reset_session,
    save_sessions,
)


def make_agent(name="claude", pane=1, hours_ago=0, failures=0):
    started = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    return {
        "name": name,
        "pane": pane,
        "cmd": "claude --dangerously-skip-permissions --continue",
        "started_at": started,
        "failures": failures,
    }


# U-S01: Load sessions.json — parse all agent entries
def test_u_s01_load_sessions(tmp_path):
    agents = [make_agent("claude", 1), make_agent("codex", 2)]
    sessions_file = tmp_path / "sessions.json"
    sessions_file.write_text(json.dumps({"agents": agents}))

    result = load_sessions(str(sessions_file))
    assert len(result) == 2
    assert result[0]["name"] == "claude"
    assert result[1]["name"] == "codex"
    assert result[0]["pane"] == 1


# U-S02: Save sessions.json — JSON matches expected structure
def test_u_s02_save_sessions(tmp_path):
    agents = [make_agent("claude", 1)]
    sessions_file = str(tmp_path / "sessions.json")

    save_sessions(sessions_file, agents)

    data = json.loads(open(sessions_file).read())
    assert "agents" in data
    assert len(data["agents"]) == 1
    assert data["agents"][0]["name"] == "claude"


# U-S03: Refresh needed — age > SESSION_MAX_HOURS → True
def test_u_s03_refresh_needed_age(tmp_path):
    agent = make_agent(hours_ago=SESSION_MAX_HOURS + 1)
    assert refresh_needed(agent) is True


# U-S04: Refresh needed — failures >= SESSION_MAX_FAILURES → True
def test_u_s04_refresh_needed_failures():
    agent = make_agent(failures=SESSION_MAX_FAILURES)
    assert refresh_needed(agent) is True


# U-S05: Refresh not needed — fresh session, 0 failures → False
def test_u_s05_refresh_not_needed():
    agent = make_agent(hours_ago=0, failures=0)
    assert refresh_needed(agent) is False


# U-S06: Increment failure count — failures +1, original unchanged
def test_u_s06_increment_failure():
    agent = make_agent(failures=1)
    updated = increment_failure(agent)
    assert updated["failures"] == 2
    assert agent["failures"] == 1  # original unchanged
    assert updated is not agent


# U-S07: Reset after refresh — started_at=now, failures=0
def test_u_s07_reset_session():
    agent = make_agent(hours_ago=10, failures=3)
    before = datetime.now(timezone.utc)
    updated = reset_session(agent)
    after = datetime.now(timezone.utc)

    assert updated["failures"] == 0
    assert updated is not agent
    started = datetime.fromisoformat(updated["started_at"])
    # normalize to UTC if naive
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    assert before <= started <= after


# U-S08: Health check — pane alive (mock tmux) → True
def test_u_s08_health_check_alive():
    agent = make_agent(pane=1)
    mock_result = MagicMock()
    mock_result.returncode = 0

    with patch("agent_crew.session.subprocess.run", return_value=mock_result) as mock_run:
        result = check_health(agent, session="crew")

    assert result is True
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert "tmux" in args
    assert "capture-pane" in args


# U-S09: Health check — pane dead (mock tmux) → False
def test_u_s09_health_check_dead():
    agent = make_agent(pane=1)
    mock_result = MagicMock()
    mock_result.returncode = 1

    with patch("agent_crew.session.subprocess.run", return_value=mock_result):
        result = check_health(agent, session="crew")

    assert result is False


# U-S10: Refresh uses cmd from sessions.json — correct tmux send-keys called
def test_u_s10_refresh_pane_uses_cmd():
    agent = make_agent(pane=2)
    agent["cmd"] = "claude --dangerously-skip-permissions --continue"

    with patch("agent_crew.session.subprocess.run") as mock_run:
        refresh_pane(agent, session="crew")

    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert "tmux" in args
    assert "send-keys" in args
    assert agent["cmd"] in args
