"""Typed provider failure evidence used by retry and escalation decisions.

Only provider-owned JSON events are interpreted. Codex supplies quota and
capacity as terminal error messages in those events; echoed prompts, pane
captures, and diagnostic log lines never classify a failure.
"""

from __future__ import annotations

import datetime
import json
import re
import time
from enum import Enum
from typing import Optional


class FailureKind(str, Enum):
    PROVIDER_LIMIT = "provider_limit"
    CAPACITY = "capacity"
    TRANSIENT = "transient"
    CONFIG_ERROR = "config_error"
    TIMEOUT = "timeout"
    AGENT_FAILED = "agent_failed"
    TEST_FAILURE = "test_failure"


def failure_kind(error_info: Optional[dict]) -> Optional[str]:
    """Return a validated class, or unknown for absent/malformed evidence."""
    kind = error_info.get("kind") if isinstance(error_info, dict) else None
    return kind if kind in {member.value for member in FailureKind} else None


_CODEX_QUOTA = re.compile(r"^\s*(?:error:\s*)?you['’]ve hit your usage limit\b", re.I)
_CODEX_CAPACITY = re.compile(r"^\s*(?:error:\s*)?selected model is at capacity\b", re.I)
_GEMINI_INVALID_MODEL = re.compile(r"^\s*invalid model selection\s*$", re.I)
_CODEX_RESET = re.compile(
    r"try again at\s+(?:(?P<month>[A-Za-z]{3,9})\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?,\s+(?P<year>\d{4})\s+)?"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>AM|PM)\b", re.I)


def _codex_reset_after(message: str, failed_at: float) -> Optional[float]:
    """Seconds until the reset stated in Codex's own error event."""
    match = _CODEX_RESET.search(message)
    if not match:
        return None
    hour, minute = int(match["hour"]), int(match["minute"])
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        return None
    hour = hour % 12 + (12 if match["ampm"].lower() == "pm" else 0)
    if match["month"]:
        try:
            month = datetime.datetime.strptime(match["month"][:3].title(), "%b").month
            reset = datetime.datetime(int(match["year"]), month, int(match["day"]),
                                      hour, minute, tzinfo=datetime.timezone.utc)
        except ValueError:
            return None
    else:
        day = datetime.datetime.fromtimestamp(failed_at, datetime.timezone.utc)
        reset = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if reset.timestamp() <= failed_at:
            reset += datetime.timedelta(days=1)
    return reset.timestamp() - failed_at if reset.timestamp() > failed_at else None


def classify_provider_record(agent: str, record: dict, *,
                             failed_at: Optional[float] = None) -> Optional[dict]:
    """Classify a provider event from fields with defined machine meanings."""
    if not isinstance(record, dict):
        return None
    if agent == "claude":
        if record.get("type") not in {"result", "error"} or record.get("is_error") is not True:
            return None
    elif agent == "codex":
        if record.get("type") not in {"error", "turn.failed"}:
            return None
    elif agent in {"gemini", "agy"}:
        if str(record.get("status") or "").upper() != "ERROR" and record.get("type") != "error":
            return None
    else:
        return None

    error = record.get("error")
    error = error if isinstance(error, dict) else {}
    code = (record.get("code") or error.get("code") or error.get("type")
            or record.get("status_code") or record.get("reason"))
    if isinstance(code, int):
        code = f"http_{code}"
    code = str(code or "").upper()
    http_status = record.get("api_error_status") or record.get("http_status")
    if http_status == 429:
        code = "HTTP_429"
    retry_after = record.get("retry_after")
    if not isinstance(retry_after, (int, float)) or isinstance(retry_after, bool) or retry_after < 0:
        retry_after = None

    # Codex emits quota/capacity as provider-owned error events with a message,
    # without a machine code. The anchored phrases are read only from those
    # events; echoed commands, pane captures, and diagnostic log lines remain
    # outside the adapter.
    if agent == "codex":
        message = error.get("message") if record.get("type") == "turn.failed" else record.get("message")
        if isinstance(message, str) and _CODEX_QUOTA.match(message):
            code = "CODEX_QUOTA_EXHAUSTED"
            if retry_after is None:
                retry_after = _codex_reset_after(
                    message, failed_at if failed_at is not None else time.time())
        elif isinstance(message, str) and _CODEX_CAPACITY.match(message):
            code = "CODEX_CAPACITY"
    elif agent in {"gemini", "agy"} and not code:
        # AGY reports this configuration failure in its terminal JSON error
        # field without a code. Keep the match anchored to that provider event.
        message = record.get("error")
        if (record.get("status") == "ERROR" and record.get("num_turns") == 0
                and isinstance(message, str) and _GEMINI_INVALID_MODEL.fullmatch(message)):
            code = "INVALID_MODEL"

    if code in {"MODEL_CAPACITY_EXHAUSTED", "CODEX_CAPACITY"}:
        kind = FailureKind.CAPACITY
        retry_after = retry_after if retry_after is not None else 120
    elif code in {"QUOTA_EXHAUSTED", "INSUFFICIENT_QUOTA", "RATE_LIMIT_EXHAUSTED",
                  "CODEX_QUOTA_EXHAUSTED", "INELIGIBLE_TIER"}:
        kind = FailureKind.PROVIDER_LIMIT
    elif code in {"HTTP_429", "RESOURCE_EXHAUSTED", "RATE_LIMITED", "THROTTLED",
                  "UNAVAILABLE", "SERVICE_UNAVAILABLE"}:
        kind = FailureKind.TRANSIENT
    elif code in {"TIMEOUT", "DEADLINE_EXCEEDED"}:
        kind = FailureKind.TIMEOUT
    elif code in {"INVALID_MODEL", "INVALID_ARGUMENT", "AUTHENTICATION_REQUIRED",
                  "CONFIG_ERROR"}:
        kind = FailureKind.CONFIG_ERROR
    else:
        kind = FailureKind.AGENT_FAILED
    return {"kind": kind.value, "provider_code": code.lower() or "unknown",
            "retry_after": retry_after}


def provider_failure_from_log(log_path: str, *, agent: str,
                              since_offset: int = 0,
                              failed_at: Optional[float] = None) -> Optional[dict]:
    """Read this attempt's provider JSON events; ignore all other log text."""
    try:
        with open(log_path, "rb") as stream:
            stream.seek(max(0, since_offset))
            data = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    failure = None
    priority = {FailureKind.AGENT_FAILED.value: 0,
                FailureKind.TRANSIENT.value: 1,
                FailureKind.TIMEOUT.value: 1,
                FailureKind.CAPACITY.value: 2,
                FailureKind.CONFIG_ERROR.value: 2,
                FailureKind.PROVIDER_LIMIT.value: 3}
    for line in data.splitlines():
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            continue
        classified = classify_provider_record(agent, record, failed_at=failed_at)
        if classified and (failure is None or
                           priority[classified["kind"]] >= priority[failure["kind"]]):
            failure = classified
    return failure
