import json
import subprocess
import time
from datetime import datetime, timezone

SESSION_MAX_HOURS: int = 24
SESSION_MAX_FAILURES: int = 2


def load_sessions(path: str) -> list[dict]:
    with open(path) as f:
        data = json.load(f)
    return data["agents"]


def save_sessions(path: str, agents: list[dict]) -> None:
    with open(path, "w") as f:
        json.dump({"agents": agents}, f, indent=2)


def refresh_needed(agent: dict) -> bool:
    started_at = agent["started_at"]
    # support both epoch float and ISO-8601 string
    if isinstance(started_at, str):
        dt = datetime.fromisoformat(started_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        epoch = dt.timestamp()
    else:
        epoch = float(started_at)

    age_hours = (time.time() - epoch) / 3600
    if age_hours > SESSION_MAX_HOURS:
        return True
    if agent["failures"] >= SESSION_MAX_FAILURES:
        return True
    return False


def increment_failure(agent: dict) -> dict:
    return {**agent, "failures": agent["failures"] + 1}


def reset_session(agent: dict) -> dict:
    return {**agent, "started_at": time.time(), "failures": 0}


def check_health(agent: dict, session: str) -> bool:
    target = f"{session}:{agent['pane']}"
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", target, "-p"],
        capture_output=True,
    )
    return result.returncode == 0


def refresh_pane(agent: dict, session: str) -> None:
    target = f"{session}:{agent['pane']}"
    subprocess.run(["tmux", "send-keys", "-t", target, "", "Enter"])
    subprocess.run(["tmux", "send-keys", "-t", target, agent["cmd"], "Enter"])
