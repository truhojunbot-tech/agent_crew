import json
import subprocess
from datetime import datetime, timezone

SESSION_MAX_HOURS: int = 8
SESSION_MAX_FAILURES: int = 3


def load_sessions(path: str) -> list[dict]:
    with open(path) as f:
        data = json.load(f)
    return data["agents"]


def save_sessions(path: str, agents: list[dict]) -> None:
    with open(path, "w") as f:
        json.dump({"agents": agents}, f, indent=2)


def refresh_needed(agent: dict) -> bool:
    started = datetime.fromisoformat(agent["started_at"])
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    age_hours = (now - started).total_seconds() / 3600
    if age_hours > SESSION_MAX_HOURS:
        return True
    if agent["failures"] >= SESSION_MAX_FAILURES:
        return True
    return False


def increment_failure(agent: dict) -> dict:
    return {**agent, "failures": agent["failures"] + 1}


def reset_session(agent: dict) -> dict:
    return {**agent, "started_at": datetime.now(timezone.utc).isoformat(), "failures": 0}


def check_health(agent: dict, session: str) -> bool:
    target = f"{session}:{agent['pane']}"
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", target, "-p"],
        capture_output=True,
    )
    return result.returncode == 0


def refresh_pane(agent: dict, session: str) -> None:
    target = f"{session}:{agent['pane']}"
    subprocess.run(
        ["tmux", "send-keys", "-t", target, agent["cmd"], "Enter"],
    )
