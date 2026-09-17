"""Claude Code JSONL implementation of the provider-neutral telemetry API."""
from __future__ import annotations

from agent_crew import claude_transcript
from agent_crew.telemetry import TaskTelemetry


class ClaudeSessionTelemetryAdapter:
    """Read the newest explicit ``message.usage`` record from Claude JSONL."""

    def __init__(self, *, home=None):
        self._home = home

    def extract(self, *, provider: str, worktree_path: str,
                provider_session_id: str) -> TaskTelemetry:
        if provider != "claude":
            return TaskTelemetry()
        path = claude_transcript.claude_session_path(
            worktree_path, home=self._home, session_id=provider_session_id)
        if path is None:
            return TaskTelemetry()
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
