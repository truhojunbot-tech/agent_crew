"""Typed provider failure evidence used by retry and escalation decisions.

Only provider-owned JSON fields are interpreted. Human text, echoed prompts,
pane captures and diagnostic messages are never a failure classification.
"""

from __future__ import annotations

import json
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


def classify_provider_record(agent: str, record: dict) -> Optional[dict]:
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
                              since_offset: int = 0) -> Optional[dict]:
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
        classified = classify_provider_record(agent, record)
        if classified and (failure is None or
                           priority[classified["kind"]] >= priority[failure["kind"]]):
            failure = classified
    return failure
