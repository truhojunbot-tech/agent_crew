"""Stream-timeout recovery tests (Issue #85).

Three behaviours pinned here:

1. The watchdog reminder message instructs the agent on three options
   (complete / failed / keep working), the second of which is the explicit
   stream-timeout recovery path.
2. The legacy rate-limit detector still recognises watchdog summaries.
3. End-to-end: a watchdog timeout never substitutes another provider (#308).
"""
from fastapi.testclient import TestClient

from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


# ---------------------------------------------------------------------------
# Reminder-message contract
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# End-to-end: watchdog timeout triggers fallback chain
# ---------------------------------------------------------------------------


def _task_payload(task_id="t1", task_type="implement"):
    return {
        "task_id": task_id,
        "task_type": task_type,
        "description": "do work",
        "branch": "main",
        "priority": 3,
        "context": {},
        "project": "",
    }


class _Idle:
    """pane_busy_fn that always reports idle — forces watchdog to time out."""

    def __call__(self, pane_id: str) -> bool:
        return False


class _Push:
    def __init__(self):
        self.calls: list = []

    def __call__(self, pane_id, text):
        self.calls.append((pane_id, text))
