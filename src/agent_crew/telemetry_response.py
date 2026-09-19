"""Parse explicit usage carried by provider command responses."""
from __future__ import annotations

import json
from typing import Any

from agent_crew.telemetry import TaskTelemetry


def _token(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _span(*values: int | None) -> int | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def response_telemetry(provider: str, response: object) -> TaskTelemetry:
    """Map only explicit provider usage fields; absent stays unknown."""
    response = _mapping(response)
    if provider == "claude":
        # Claude stream-json's terminal result is the authoritative span-wide
        # output count.  Its per-message usage is correct for input/cache but
        # reports only a tiny final-message output slice.
        if response.get("type") == "result":
            usage = _mapping(response.get("usage"))
            return TaskTelemetry(
                output_tokens=_token(usage.get("output_tokens")),
                reasoning_tokens=_token(usage.get("reasoning_tokens")),
            )
        message = _mapping(response.get("message"))
        usage = _mapping(message.get("usage"))
        uncached = _token(usage.get("input_tokens"))
        write, read = _token(usage.get("cache_creation_input_tokens")), _token(usage.get("cache_read_input_tokens"))
        return TaskTelemetry(uncached_input_tokens=uncached, cache_write_tokens=write,
                             cache_read_tokens=read, output_tokens=_token(usage.get("output_tokens")),
                             reasoning_tokens=_token(usage.get("reasoning_tokens")),
                             context_window_tokens=_span(uncached, write, read),
                             model=message.get("model") if isinstance(message.get("model"), str) else None,
                             provider_session_id=response.get("session_id") if isinstance(response.get("session_id"), str) else None)
    if provider == "codex":
        usage = _mapping(response.get("usage"))
        raw, read = _token(usage.get("input_tokens")), _token(usage.get("cached_input_tokens"))
        uncached = raw - read if raw is not None and read is not None else raw
        write = _token(usage.get("cache_write_input_tokens"))
        return TaskTelemetry(uncached_input_tokens=uncached, cache_write_tokens=write,
                             cache_read_tokens=read, output_tokens=_token(usage.get("output_tokens")),
                             reasoning_tokens=_token(usage.get("reasoning_output_tokens")),
                             context_window_tokens=_span(uncached, write, read))
    if provider == "gemini":
        result = _mapping(response.get("response")) or response
        usage = _mapping(result.get("usageMetadata")) or _mapping(result.get("usage_metadata"))
        raw, read = _token(usage.get("promptTokenCount")), _token(usage.get("cachedContentTokenCount"))
        uncached = raw - read if raw is not None and read is not None else raw
        write = _token(usage.get("cacheCreationTokenCount"))
        return TaskTelemetry(uncached_input_tokens=uncached, cache_write_tokens=write,
                             cache_read_tokens=read, output_tokens=_token(usage.get("candidatesTokenCount")),
                             reasoning_tokens=_token(usage.get("thoughtsTokenCount")),
                             context_window_tokens=_span(uncached, write, read),
                             model=result.get("modelVersion") if isinstance(result.get("modelVersion"), str) else None)
    return TaskTelemetry()


def response_log_telemetry(provider: str, text: str) -> TaskTelemetry:
    """Aggregate explicit JSONL usage from a single dispatch-log suffix."""
    fields = ("uncached_input_tokens", "cache_write_tokens", "cache_read_tokens",
              "output_tokens", "reasoning_tokens", "context_window_tokens")
    totals: dict[str, int | None] = {field: None for field in fields}
    final_values: dict[str, int | None] = {}
    seen: set[str] = set()
    model = session = None
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        is_terminal_claude_result = provider == "claude" and record.get("type") == "result"
        message = _mapping(record.get("message"))
        identity = message.get("id") if provider == "claude" else record.get("id")
        if isinstance(identity, str) and identity:
            if identity in seen:
                continue
            seen.add(identity)
        item = response_telemetry(provider, record)
        for field in fields:
            value = getattr(item, field)
            if value is not None:
                if is_terminal_claude_result and field in ("output_tokens", "reasoning_tokens"):
                    final_values[field] = value
                else:
                    totals[field] = (totals[field] or 0) + value
        model = item.model or model
        session = item.provider_session_id or session
    totals.update(final_values)
    return TaskTelemetry(**totals, model=model, provider_session_id=session)
