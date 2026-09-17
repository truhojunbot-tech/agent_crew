"""Provider-neutral task telemetry contract.

Providers may expose different transcript formats, but the queue stores only
these directly observed values. ``None`` means the provider did not supply the
fact; callers must never derive or estimate it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class TaskTelemetry:
    uncached_input_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    context_window_tokens: Optional[int] = None
    stable_prefix_hash: Optional[str] = None
    context_pack_hash: Optional[str] = None
    model: Optional[str] = None
    provider_session_id: Optional[str] = None


class TaskTelemetryAdapter(Protocol):
    """Extract provider observations for one actual task session."""

    def extract(
        self, *, provider: str, worktree_path: str, provider_session_id: str
    ) -> TaskTelemetry:
        ...


class NullTaskTelemetryAdapter:
    """Portable fallback when no provider-specific transcript is available."""

    def extract(
        self, *, provider: str, worktree_path: str, provider_session_id: str
    ) -> TaskTelemetry:
        return TaskTelemetry()


# Kept as a facade for existing core callers; the Claude JSONL reader itself is
# isolated in ``telemetry_claude`` rather than spread through queue/server flow.
from agent_crew.telemetry_claude import ClaudeSessionTelemetryAdapter  # noqa: E402


def default_telemetry_adapter() -> TaskTelemetryAdapter:
    """Return the bundled adapter; non-Claude tasks yield unknown values."""
    return ClaudeSessionTelemetryAdapter()
