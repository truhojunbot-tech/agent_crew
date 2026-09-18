"""Provider-neutral, durable project role assignment (#337)."""

from __future__ import annotations

import logging
from collections.abc import Mapping


logger = logging.getLogger(__name__)

ROLE_ORDER = ("implementer", "reviewer", "tester")
DEFAULT_ROLE_TO_AGENT = {
    "implementer": "claude",
    "reviewer": "codex",
    "tester": "gemini",
}
EXPLICIT_SOURCE = "explicit project config"
LEGACY_SOURCE = "legacy state.json.roles"
DEFAULT_SOURCE = "hardcoded default"


def validate_explicit_role_agents(mapping: Mapping[str, object]) -> dict[str, str]:
    """Validate a complete explicit mapping without imposing provider policy."""
    if set(mapping) != set(ROLE_ORDER):
        raise ValueError("role_agents must set implementer, reviewer, and tester exactly once")
    result = {}
    for role in ROLE_ORDER:
        agent = mapping[role]
        if not isinstance(agent, str) or not agent.strip():
            raise ValueError(f"agent for {role!r} must be a non-empty string")
        result[role] = agent.strip()
    return result


def effective_role_mapping(
    state: Mapping[str, object] | None, *, project: str = "unknown",
) -> tuple[dict[str, str], str]:
    """Resolve explicit, legacy, then hardcoded mappings without migration."""
    if not isinstance(state, Mapping):
        return dict(DEFAULT_ROLE_TO_AGENT), DEFAULT_SOURCE

    explicit = state.get("role_agents")
    if explicit is not None:
        try:
            if not isinstance(explicit, Mapping):
                raise ValueError("role_agents must be an object")
            return validate_explicit_role_agents(explicit), EXPLICIT_SOURCE
        except ValueError as exc:
            logger.warning(
                "project %s has invalid role_agents (%s); retaining legacy/default mapping",
                project, exc,
            )

    result = dict(DEFAULT_ROLE_TO_AGENT)
    legacy = state.get("roles")
    if isinstance(legacy, list):
        found = False
        for entry in legacy:
            if not isinstance(entry, Mapping):
                continue
            role, agent = entry.get("role"), entry.get("agent")
            if role in ROLE_ORDER and isinstance(agent, str) and agent.strip():
                result[role] = agent.strip()
                found = True
        if found:
            return result, LEGACY_SOURCE
    return result, DEFAULT_SOURCE
