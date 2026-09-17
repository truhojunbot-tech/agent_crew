"""Claude Code JSONL implementation of the provider-neutral telemetry API."""
from __future__ import annotations

import json
import pathlib
import re
from typing import Optional

from agent_crew.telemetry import TaskTelemetry

_TAIL_CHUNK = 1 << 20
_TAIL_CHUNKS = 16


def _claude_home(home=None) -> pathlib.Path:
    return pathlib.Path(home) if home else pathlib.Path.home() / ".claude"


def _token(value) -> Optional[int]:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class ClaudeSessionTelemetryAdapter:
    """Read the newest explicit ``message.usage`` record from Claude JSONL."""

    def __init__(self, *, home=None):
        self._home = home

    def _session_path(self, worktree_path: str, provider_session_id: str):
        if not worktree_path:
            return None
        directory = _claude_home(self._home) / "projects" / re.sub(r"[/._]", "-", worktree_path)
        try:
            if provider_session_id:
                path = directory / f"{provider_session_id}.jsonl"
                return path if path.is_file() else None
            sessions = sorted(directory.glob("*.jsonl"), key=lambda path: path.stat().st_mtime,
                              reverse=True)
            return sessions[0] if sessions else None
        except OSError:
            return None

    def extract(self, *, provider: str, worktree_path: str,
                provider_session_id: str) -> TaskTelemetry:
        if provider != "claude":
            return TaskTelemetry()
        path = self._session_path(worktree_path, provider_session_id)
        if path is None:
            return TaskTelemetry()
        try:
            with path.open("rb") as transcript:
                end, buffer = path.stat().st_size, b""
                for _ in range(_TAIL_CHUNKS):
                    start = max(0, end - _TAIL_CHUNK)
                    transcript.seek(start)
                    buffer = transcript.read(end - start) + buffer
                    end = start
                    for line in reversed(buffer.split(b"\n")):
                        try:
                            entry = json.loads(line)
                        except Exception:
                            continue
                        message = entry.get("message") or {}
                        usage = message.get("usage")
                        if not isinstance(usage, dict):
                            continue
                        uncached = _token(usage.get("input_tokens"))
                        cache_write = _token(usage.get("cache_creation_input_tokens"))
                        cache_read = _token(usage.get("cache_read_input_tokens"))
                        values = (uncached, cache_write, cache_read)
                        context = sum(value for value in values if value is not None) if any(
                            value is not None for value in values) else None
                        return TaskTelemetry(
                            uncached_input_tokens=uncached,
                            cache_write_tokens=cache_write,
                            cache_read_tokens=cache_read,
                            output_tokens=_token(usage.get("output_tokens")),
                            reasoning_tokens=_token(usage.get("reasoning_tokens")),
                            context_window_tokens=context,
                            model=message.get("model") if isinstance(message.get("model"), str) else None,
                            provider_session_id=path.stem,
                        )
                    if start == 0:
                        break
        except (OSError, ValueError):
            pass
        return TaskTelemetry()
