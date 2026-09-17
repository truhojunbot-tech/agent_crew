"""Claude Code JSONL implementation of the provider-neutral telemetry API."""
from __future__ import annotations

import pathlib

from agent_crew import claude_transcript
from agent_crew.telemetry import TaskTelemetry


class ClaudeSessionTelemetryAdapter:
    """Observe explicit Claude JSONL usage for a task's recorded span."""

    def __init__(self, *, home=None):
        self._home = home

    def extract(self, *, provider: str, worktree_path: str,
                provider_session_id: str) -> TaskTelemetry:
        if provider != "claude":
            return TaskTelemetry()
        boundary = getattr(provider_session_id, "claude_transcript_start", None)
        boundary_session = ""
        offset = None
        if isinstance(boundary, dict):
            candidate = boundary.get("session_id")
            candidate_offset = boundary.get("offset")
            if (isinstance(candidate, str) and isinstance(candidate_offset, int)
                    and not isinstance(candidate_offset, bool) and candidate_offset >= 0):
                boundary_session, offset = candidate, candidate_offset
        # A fresh Claude invocation has no transcript id until Claude creates
        # it. It is observable only when the persisted dispatch snapshot proves
        # exactly one transcript path appeared in the same project directory.
        if offset is not None and not boundary_session:
            snapshot = boundary.get("fresh_session_paths") if isinstance(boundary, dict) else None
            if not isinstance(snapshot, list) or not all(isinstance(path, str) for path in snapshot):
                return TaskTelemetry()
            current_paths = claude_transcript.claude_session_paths(
                worktree_path, home=self._home)
            if not isinstance(current_paths, list):
                return TaskTelemetry()
            current = set(current_paths)
            created = current - set(snapshot)
            if len(created) != 1:
                return TaskTelemetry()
            return self._extract_span(pathlib.Path(next(iter(created))), 0)
        path = claude_transcript.claude_session_path(
            worktree_path, home=self._home,
            session_id=boundary_session or provider_session_id)
        if path is None:
            return TaskTelemetry()
        if offset is not None:
            return self._extract_span(path, offset)
        message = claude_transcript.last_message(path)
        if message is None:
            return TaskTelemetry()
        usage = message["usage"]
        return TaskTelemetry(
            uncached_input_tokens=claude_transcript.token(usage.get("input_tokens")),
            cache_write_tokens=claude_transcript.token(usage.get("cache_creation_input_tokens")),
            cache_read_tokens=claude_transcript.token(usage.get("cache_read_input_tokens")),
            output_tokens=claude_transcript.token(usage.get("output_tokens")),
            reasoning_tokens=claude_transcript.token(usage.get("reasoning_tokens")),
            context_window_tokens=claude_transcript.usage_context_tokens(usage),
            model=message.get("model") if isinstance(message.get("model"), str) else None,
            provider_session_id=path.stem,
        )

    @staticmethod
    def _extract_span(path, offset: int) -> TaskTelemetry:
        """Sum uniquely identified provider invocations from one task span."""
        totals = {
            "input_tokens": None,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": None,
            "output_tokens": None,
            "reasoning_tokens": None,
        }
        seen = set()
        model = None
        for record in claude_transcript.messages_from_offset(path, offset):
            entry, message = record["entry"], record["message"]
            # Claude transcripts retain message.id when replaying the same
            # invocation; top-level uuid is a fallback for formats without it.
            identity = message.get("id") or entry.get("uuid")
            if not isinstance(identity, str) or not identity or identity in seen:
                if isinstance(identity, str) and identity:
                    continue
            else:
                seen.add(identity)
            usage = message["usage"]
            for field in totals:
                value = claude_transcript.token(usage.get(field))
                if value is not None:
                    totals[field] = (totals[field] or 0) + value
            if isinstance(message.get("model"), str):
                model = message["model"]
        context_values = (
            totals["cache_read_input_tokens"],
            totals["cache_creation_input_tokens"],
            totals["input_tokens"],
        )
        return TaskTelemetry(
            uncached_input_tokens=totals["input_tokens"],
            cache_write_tokens=totals["cache_creation_input_tokens"],
            cache_read_tokens=totals["cache_read_input_tokens"],
            output_tokens=totals["output_tokens"],
            reasoning_tokens=totals["reasoning_tokens"],
            context_window_tokens=(sum(value for value in context_values if value is not None)
                                   if any(value is not None for value in context_values) else None),
            model=model,
            provider_session_id=path.stem,
        )
