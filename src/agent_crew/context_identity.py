"""Durable context identity + lifecycle telemetry (#202).

Agent Crew intentionally reuses provider CLI conversations (`claude
--continue`, `codex exec resume --last`, `agy --continue`) across tasks for
the life of a role's worktree. This module gives that reuse a stable,
observable identity — separate from `task_id`, `role`, and `agent` — so an
external tool can answer "which task used which context, did it resume or
start fresh, and what happened to it" without Agent Crew depending on any
quota/analytics product.

Design principle: **Agent ≠ Role ≠ Context.** A context is scoped by
``(project, agent, worktree_path)`` — not by role — because
`agent_override` can route a task from one role into another agent's
worktree, and doing so genuinely resumes *that* agent's ongoing
conversation regardless of which role nominally owns the task. The durable
scheduling/DB side of this lives in ``TaskQueue.get_or_create_context`` in
``queue.py``; this module holds the provider-agnostic helpers: the JSONL
event writer and the two best-effort log-parsing heuristics.

See ``docs/context_identity_contract.md`` for the full field/event
contract and versioning policy.
"""
from __future__ import annotations

import datetime
import json
import re
from typing import Optional

# Bump only for breaking changes to the attribution/event field contract.
# Additive fields do not require a bump.
CONTEXT_SCHEMA_VERSION = 1

_CLAUDE_SESSION_ID_RE = re.compile(r'"session_id"\s*:\s*"([0-9a-fA-F-]{8,})"')

# Best-effort only (#202 non-goal: no deep CLI protocol parsing). None of
# claude/codex/agy currently emit a structured "context compacted" event on
# stdout that Agent Crew can rely on, so this is a plain substring scan of
# whatever free-text the CLI happens to print. False negatives are expected
# and acceptable — this event is documented as observational, not a
# guarantee.
_COMPACTION_MARKERS = (
    "context was compacted",
    "conversation compacted",
    "compacting the conversation",
    "compacting conversation",
)


def extract_claude_session_id(log_text: str) -> Optional[str]:
    """Best-effort extraction of claude's own session id from stream-json output.

    ``claude -p ... --output-format stream-json`` emits a system/init line
    containing a ``"session_id"`` field (the same id ``claude --resume
    <id>`` would take). codex and agy do not reliably expose an equivalent
    on stdout today, so this only ever returns non-None for claude output —
    callers must treat ``provider_session_id`` as nullable for other
    providers (#202 scope).
    """
    if not log_text:
        return None
    m = _CLAUDE_SESSION_ID_RE.search(log_text)
    return m.group(1) if m else None


def detect_context_compaction(log_text: str) -> bool:
    """Best-effort, case-insensitive scan for a provider-reported compaction.

    Observational only — see module docstring. A miss here does not mean a
    compaction didn't happen, only that Agent Crew didn't see a recognized
    marker for it.
    """
    if not log_text:
        return False
    lowered = log_text.lower()
    return any(marker in lowered for marker in _COMPACTION_MARKERS)


def record_context_event(events_path: str, event_type: str, **fields) -> None:
    """Append one versioned, timestamped event line to the context lifecycle
    JSONL stream at ``events_path``.

    Kept as a separate file from ``attribution.jsonl`` on purpose:
    attribution.jsonl has one fixed per-task shape that existing external
    consumers may already parse positionally/by-key; mixing in
    heterogeneously-shaped lifecycle events would be a breaking change for
    them. This stream is purely additive.

    Safety: callers must never pass credentials, full prompts, source code,
    or conversation contents as a field value (#202 privacy requirement) —
    this is metadata-only telemetry.
    """
    payload = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "event_type": event_type,
        "ts": datetime.datetime.utcnow().isoformat(),
        **fields,
    }
    with open(events_path, "a") as f:
        f.write(json.dumps(payload) + "\n")


def append_attribution_jsonl(jsonl_path: str, attribution_row: dict) -> None:
    """Append one line to ``attribution.jsonl`` mirroring a
    ``task_attribution`` DB row verbatim (whatever ``TaskQueue.
    get_attribution()`` returns).

    Called at least twice per task — once at dispatch time
    (``status="in_progress"``) and once more when it reaches a terminal
    state (``status`` + ``outcome`` + ``completed_at`` populated) — so a
    tail-only consumer that never touches the DB can still observe the
    final outcome, not just the in-flight snapshot (#202 review finding:
    PR #203 originally wrote only the dispatch-time line). Consumers
    should treat this as one row per ``(task_id, updated_at)`` and take the
    most recent line per ``task_id`` as current state, same as they would
    reading the DB row directly — this function deliberately reuses the
    DB row shape instead of hand-building a separate dict so the two can
    never drift apart.
    """
    with open(jsonl_path, "a") as f:
        f.write(json.dumps(attribution_row) + "\n")
