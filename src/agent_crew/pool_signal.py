"""Quota pool admission signal for Claude implement/fix rounds (#1033).

Quota owns the pace signal file (``DEFAULT_POOL_SIGNAL_PATH``); enforcing it
is the crew runtime's job. Before this, the pipeline auto-spawned review→fix rounds on
``claude_p`` during a non-Athena Claude hold and coordinators could only cancel
after Claude was already spent (8105 ``fix-review-impl-924d-r0-r1``, 11-17).

Semantics (Quota bb 20261118t093416z, mirroring ``pace_alert.admissions_cycle_state``):

- ``balance.action`` ``hold_admissions`` → hold, ``reopen_admissions`` → open;
  the action wins over the rule.
- Otherwise ``balance.numbers.admissions_rule`` ``hold`` | ``open``. A null
  rule means *no change*: the last valid state persisted for this project
  stays in force.
- Anything else is UNKNOWN, and UNKNOWN is not OPEN: a missing, malformed or
  stale (``generated_at`` older than ``FRESHNESS_SECONDS``) file, or a null
  rule with no persisted state (cold start). The gate parks on UNKNOWN.

Every gate decision re-reads the file; nothing is cached, so a stale ``open``
can never authorise a dispatch. Per-pool ``admissions_effective``: see ``POOLS``.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime
from typing import Optional

from agent_crew.role_mapping import provider_family

POOL_SIGNAL_ENV = "AGENT_CREW_POOL_SIGNAL_PATH"
DEFAULT_POOL_SIGNAL_PATH = "~/alfred/quota/pace_signal.json"
#: Parked rows wait in this status: not needs_human, not failed.
WAITING_POOL_STATUS = "waiting_pool"
#: Quota writes the file every 15 min; older than this is UNKNOWN.
FRESHNESS_SECONDS = 60 * 60.0
#: Same-host writer: a ``generated_at`` further ahead than this is malformed.
FUTURE_SKEW_SECONDS = 5 * 60.0
#: Typed reason for a park on an UNKNOWN signal.
UNKNOWN_REASON = "POOL_SIGNAL_UNKNOWN"
#: Last valid state, per project, under the crew state dir.
STATE_FILENAME = "pool_signal_state.json"
HOLD, OPEN, UNKNOWN = "hold", "open", "unknown"
#: Projects the Claude hold does not apply to.
EXEMPT_PROJECTS = frozenset({"athena"})
#: Task types that are Claude implement/fix rounds when bound to Claude.
GATED_TASK_TYPES = frozenset({"implement", "fix"})
#: Provider pools Quota publishes in ``balance.numbers.admissions_effective``
#: (agent_crew#1033 c7034905225). When present it is the per-pool verdict and
#: wins over ``balance.action`` / ``admissions_rule``: exactly ``open`` or
#: ``hold`` is valid, anything else (unknown string, null, wrong type, missing
#: pool key, a non-object map) is HOLD, never open. Without it the legacy
#: single rule applies to the Claude pool only; on a fresh, valid legacy file
#: the Codex pool dispatches as before. A missing, malformed, stale or
#: future-dated file is UNKNOWN for every pool, Codex included.
CLAUDE_POOL, CODEX_POOL = "claude", "codex"
POOLS = (CLAUDE_POOL, CODEX_POOL)
#: Typed reason for a park on a present but invalid ``admissions_effective``.
INVALID_EFFECTIVE_REASON = "POOL_EFFECTIVE_INVALID"

_state_lock = threading.Lock()


def pool_signal_path() -> str:
    return os.path.expanduser(os.getenv(POOL_SIGNAL_ENV) or DEFAULT_POOL_SIGNAL_PATH)


def _unknown(detail: str, path: str) -> dict:
    return {"rule": UNKNOWN, "reason": f"{UNKNOWN_REASON}: {detail}"[:300],
            "path": path, "generated_at": None}


def _parse_ts(value: object) -> Optional[float]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _norm(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


def _ungated(pool: str, detail: str, path: str, generated_at: object) -> dict:
    """The Codex pool without per-pool data: not gated, as before #1033's
    consumer round. Not persisted as a last valid state."""
    return {"rule": OPEN, "reason": f"{pool} pool not gated: {detail}"[:300], "path": path,
            "generated_at": generated_at, "pool": pool, "ungated": True}


def _read_effective(numbers: dict, pool: str, path: str, generated_at: object) -> dict:
    """``admissions_effective[pool]``: exactly open or hold, else HOLD."""
    effective = numbers["admissions_effective"]

    def per_pool(key: str) -> str:
        value = numbers.get(key)
        return str(value.get(pool) or "") if isinstance(value, dict) else ""

    if not isinstance(effective, dict):
        detail = f"admissions_effective={effective!r} is not an object"
    elif pool not in effective:
        detail = f"admissions_effective has no {pool!r} key"
    elif effective[pool] not in (HOLD, OPEN):
        detail = f"admissions_effective[{pool!r}]={effective[pool]!r} is not open|hold"
    else:
        rule = effective[pool]
        source = per_pool("admissions_effective_source")
        reason = (f"admissions_effective[{pool}]={rule}"
                  + (f" source={source}" if source else "")
                  + f": {per_pool('admissions_effective_reason')}")
        return {"rule": rule, "reason": reason[:300], "path": path,
                "generated_at": generated_at, "pool": pool}
    return {"rule": HOLD, "reason": f"{INVALID_EFFECTIVE_REASON}: {detail}; parked as hold"[:300],
            "path": path, "generated_at": generated_at, "pool": pool, "invalid": True}


def _read_file(path: str, now: float, pool: str = CLAUDE_POOL) -> dict:
    """One read of the file for ``pool``. ``rule`` is hold | open | unknown,
    or ``None`` for a fresh, valid legacy file whose rule is null (no change)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        balance = data["balance"]
        numbers = balance["numbers"]
        generated_raw = data["generated_at"]
        if not isinstance(numbers, dict):
            raise TypeError("balance.numbers is not an object")
    except FileNotFoundError:
        return _unknown("signal file missing", path)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _unknown(f"malformed: {type(exc).__name__}: {exc}", path)
    generated = _parse_ts(generated_raw)
    if generated is None:
        return _unknown(f"malformed: generated_at={generated_raw!r}", path)
    age = now - generated
    if age > FRESHNESS_SECONDS:
        return _unknown(f"stale: generated_at {generated_raw} is {int(age // 60)} min old", path)
    if age < -FUTURE_SKEW_SECONDS:
        return _unknown(f"malformed: generated_at {generated_raw} is in the future", path)
    if "admissions_effective" in numbers:
        return _read_effective(numbers, pool, path, generated_raw)
    if pool != CLAUDE_POOL:
        return _ungated(pool, "no admissions_effective in the signal", path, generated_raw)
    action = _norm(balance.get("action"))
    rule = _norm(numbers.get("admissions_rule"))
    reason = numbers.get("admissions_rule_reason") or ""
    if action == "hold_admissions":
        rule, reason = HOLD, balance.get("reason") or reason
    elif action == "reopen_admissions":
        rule, reason = OPEN, balance.get("reason") or reason
    elif rule is not None and rule not in (HOLD, OPEN):
        return _unknown(f"malformed: admissions_rule={rule!r}", path)
    return {"rule": rule, "reason": str(reason)[:300], "path": path,
            "generated_at": generated_raw}


def _state_file(state_dir: Optional[str]) -> Optional[str]:
    return os.path.join(state_dir, STATE_FILENAME) if state_dir else None


def _record(data: object) -> Optional[dict]:
    return data if isinstance(data, dict) and data.get("rule") in (HOLD, OPEN) else None


def _load_pools(state_dir: Optional[str]) -> dict:
    """``{pool: record}``. The pre-per-pool single-rule shape applies to
    every pool."""
    path = _state_file(state_dir)
    if path is None:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if isinstance(data, dict) and isinstance(data.get("pools"), dict):
        return {p: r for p in POOLS if (r := _record(data["pools"].get(p))) is not None}
    legacy = _record(data)
    return {p: legacy for p in POOLS} if legacy is not None else {}


def load_last_valid(state_dir: Optional[str], pool: str = CLAUDE_POOL) -> Optional[dict]:
    return _load_pools(state_dir).get(pool)


def _save_last_valid(state_dir: Optional[str], signal: dict,
                     pool: str = CLAUDE_POOL) -> None:
    path = _state_file(state_dir)
    if path is None or state_dir is None:
        return
    record = {"rule": signal["rule"], "reason": signal["reason"],
              "generated_at": signal["generated_at"]}
    pools = _load_pools(state_dir)
    if pools.get(pool) == record:
        return
    pools[pool] = record
    os.makedirs(state_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=".pool_signal_state.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"pools": pools}, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_pool_signal(*, state_dir: Optional[str] = None,
                     now: Optional[float] = None, pool: str = CLAUDE_POOL) -> dict:
    """Return ``{"rule": "hold"|"open"|"unknown", "reason", "path", "generated_at"}``
    for ``pool``.

    Re-reads the file on every call. A fresh hold/open is persisted as the
    pool's last valid state in ``state_dir``; a fresh null legacy rule falls
    back to it.
    """
    path = pool_signal_path()
    now = time.time() if now is None else now
    # UNKNOWN stays UNKNOWN for every pool: only a fresh, valid legacy file
    # without ``admissions_effective`` leaves Codex ungated (review-impl-1033d-r0).
    signal = _read_file(path, now, pool)
    with _state_lock:
        if signal["rule"] in (HOLD, OPEN):
            if not (signal.get("invalid") or signal.get("ungated")):
                _save_last_valid(state_dir, signal, pool)
            return signal
        if signal["rule"] is None:
            last = load_last_valid(state_dir, pool)
            if last is None:
                return _unknown("admissions_rule null with no last valid state (cold start)",
                                path)
            return {"rule": last["rule"],
                    "reason": f"admissions_rule null: last valid {last['rule']} "
                              f"({last.get('reason') or ''})"[:300],
                    "path": path, "generated_at": signal["generated_at"]}
    return signal


def is_exempt_project(*projects: object) -> bool:
    return any(isinstance(p, str) and p.strip().lower() in EXEMPT_PROJECTS
               for p in projects)


def is_claude_round(task_type: object, agent: object) -> bool:
    """A Claude implement/fix round: ``claude`` (claude_p) or ``claude_cloud``.
    Reviews, script tests, ChatGPT and Codex are never gated."""
    return task_type in GATED_TASK_TYPES and provider_family(agent) == "claude"


def round_pool(task_type: object, agent: object) -> Optional[str]:
    """The provider pool an implement/fix round spends: ``claude`` for
    claude_p / claude_cloud, ``codex`` for Codex, else ``None`` (not gated:
    reviews, script tests, ChatGPT, gemini)."""
    if task_type not in GATED_TASK_TYPES:
        return None
    family = provider_family(agent)
    return family if family in POOLS else None
