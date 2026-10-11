"""Best-effort event-driven quota contract emission (#750)."""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
import threading
import time

logger = logging.getLogger(__name__)
_guard = threading.Condition()
_pending: dict[str, bool] = {}


def _emit(command: str, project: str) -> None:
    argv = [part.replace("{project}", project) for part in shlex.split(command)]
    if not argv:
        raise ValueError("AGENT_CREW_CONTRACT_EMIT_CMD is empty")
    env = {**os.environ, "AGENT_CREW_CONTRACT_PROJECT": project}
    completed = subprocess.run(argv, env=env, capture_output=True, text=True,
                               timeout=120, check=False)
    if completed.returncode:
        raise RuntimeError(f"exit {completed.returncode}: {completed.stderr[-500:]}")


def _worker(command: str, project: str) -> None:
    while True:
        try:
            _emit(command, project)
        except Exception:
            logger.exception("quota contract emit failed for project=%s", project)
        with _guard:
            if _pending[project]:
                _pending[project] = False
            else:
                del _pending[project]
                _guard.notify_all()
                return


def schedule_emit(project: str) -> bool:
    """Start an emitter off-thread, or schedule one retry after an active emit."""
    command = os.getenv("AGENT_CREW_CONTRACT_EMIT_CMD", "").strip()
    if not command or not project:
        return False
    with _guard:
        if project in _pending:
            _pending[project] = True
            return False
        _pending[project] = False
        try:
            threading.Thread(target=_worker, args=(command, project), daemon=True,
                             name=f"contract-emit-{project}").start()
        except Exception:
            del _pending[project]
            logger.exception("could not launch quota contract emit for project=%s", project)
            return False
    return True


def in_flight(project: str) -> int:
    with _guard:
        return int(project in _pending)


def wait_idle(project: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    with _guard:
        while project in _pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _guard.wait(remaining)
        return True
