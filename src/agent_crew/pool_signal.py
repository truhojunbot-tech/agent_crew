"""Quota pool admission signal for Claude implement/fix rounds (#1033).

Quota owns ``~/alfred/quota/pace_signal.json``; enforcing it is the crew
runtime's job. Before this, the pipeline auto-spawned review→fix rounds on
``claude_p`` during a non-Athena Claude hold and coordinators could only cancel
after Claude was already spent (8105 ``fix-review-impl-924d-r0-r1``, 11-17).

One reader, one short cache. A missing, unreadable or malformed file fails
OPEN (``rule == "unreadable"``): a broken file must not stall every crew.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Optional

from agent_crew.role_mapping import provider_family

POOL_SIGNAL_ENV = "AGENT_CREW_POOL_SIGNAL_PATH"
DEFAULT_POOL_SIGNAL_PATH = os.path.join("~", "alfred", "quota", "pace_signal.json")
#: Parked rows wait in this status: not needs_human, not failed.
WAITING_POOL_STATUS = "waiting_pool"
CACHE_SECONDS = 60.0
#: Projects the Claude hold does not apply to.
EXEMPT_PROJECTS = frozenset({"athena"})
#: Task types that are Claude implement/fix rounds when bound to Claude.
GATED_TASK_TYPES = frozenset({"implement", "fix"})

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def pool_signal_path() -> str:
    return os.path.expanduser(os.getenv(POOL_SIGNAL_ENV) or DEFAULT_POOL_SIGNAL_PATH)


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        numbers = data["balance"]["numbers"]
        rule = numbers["admissions_rule"]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"rule": "unreadable", "reason": f"{type(exc).__name__}: {exc}"[:200],
                "path": path}
    rule = rule.strip().lower() if isinstance(rule, str) else rule
    if rule not in ("hold", "open"):
        return {"rule": "unreadable", "reason": f"admissions_rule={rule!r}"[:200],
                "path": path}
    reason = numbers.get("admissions_rule_reason") if isinstance(numbers, dict) else ""
    return {"rule": rule, "reason": str(reason or "")[:300], "path": path}


def read_pool_signal(*, now: Optional[float] = None) -> dict:
    """Return ``{"rule": "hold"|"open"|"unreadable", "reason", "path"}``."""
    path = pool_signal_path()
    now = time.monotonic() if now is None else now
    with _cache_lock:
        hit = _cache.get(path)
        if hit is not None and 0 <= now - hit[0] < CACHE_SECONDS:
            return dict(hit[1])
    signal = _read(path)
    with _cache_lock:
        _cache[path] = (now, signal)
    return dict(signal)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def is_exempt_project(*projects: object) -> bool:
    return any(isinstance(p, str) and p.strip().lower() in EXEMPT_PROJECTS
               for p in projects)


def is_claude_round(task_type: object, agent: object) -> bool:
    """A Claude implement/fix round: ``claude`` (claude_p) or ``claude_cloud``.
    Reviews, script tests, ChatGPT and Codex are never gated."""
    return task_type in GATED_TASK_TYPES and provider_family(agent) == "claude"
