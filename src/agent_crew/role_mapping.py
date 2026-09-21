"""Provider-neutral, durable project role assignment (#337)."""

from __future__ import annotations

import logging
from collections.abc import Mapping


logger = logging.getLogger(__name__)

ROLE_ORDER = ("implementer", "reviewer", "tester")
DEFAULT_ROLE_TO_AGENT = {
    "implementer": "codex",
    "reviewer": "claude",
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


def _legacy_role_agents(state: Mapping[str, object]) -> dict[str, str]:
    """Return only the valid role values explicitly present in legacy roles."""
    legacy = state.get("roles")
    if not isinstance(legacy, list):
        return {}
    return {
        entry["role"]: entry["agent"].strip()
        for entry in legacy
        if isinstance(entry, Mapping)
        and entry.get("role") in ROLE_ORDER
        and isinstance(entry.get("agent"), str)
        and entry["agent"].strip()
    }


def role_mapping_drift(
    state: Mapping[str, object] | None, *, project: str = "unknown",
) -> tuple[dict[str, str], str, list[str]]:
    """Resolve mapping and return fleet-visible reasons it is unsafe to trust."""
    mapping, source = effective_role_mapping(state, project=project)
    issues: list[str] = []
    if mapping != DEFAULT_ROLE_TO_AGENT:
        rendered = ", ".join(f"{role}={mapping[role]}" for role in ROLE_ORDER)
        issues.append(f"effective mapping deviates from canonical policy: {rendered}")

    if not isinstance(state, Mapping):
        return mapping, source, issues
    explicit = state.get("role_agents")
    if explicit is not None:
        try:
            if not isinstance(explicit, Mapping):
                raise ValueError("role_agents must be an object")
            explicit_mapping = validate_explicit_role_agents(explicit)
        except ValueError as exc:
            issues.append(f"invalid explicit role_agents: {exc}")
        else:
            legacy = _legacy_role_agents(state)
            disagreements = [
                f"{role}: legacy={legacy[role]}, explicit={explicit_mapping[role]}"
                for role in ROLE_ORDER
                if role in legacy and legacy[role] != explicit_mapping[role]
            ]
            if disagreements:
                issues.append(
                    "role_agents wins over legacy roles, but they disagree: "
                    + "; ".join(disagreements)
                )
    return mapping, source, issues


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
            explicit_mapping = validate_explicit_role_agents(explicit)
            legacy = _legacy_role_agents(state)
            disagreements = [
                role for role in ROLE_ORDER
                if role in legacy and legacy[role] != explicit_mapping[role]
            ]
            if disagreements:
                logger.warning(
                    "project %s role_agents wins over disagreeing legacy roles: %s",
                    project, ", ".join(disagreements),
                )
            return explicit_mapping, EXPLICIT_SOURCE
        except ValueError as exc:
            logger.warning(
                "project %s has invalid role_agents (%s); retaining legacy/default mapping",
                project, exc,
            )

    result = dict(DEFAULT_ROLE_TO_AGENT)
    legacy = _legacy_role_agents(state)
    if legacy:
        result.update(legacy)
        return result, LEGACY_SOURCE
    return result, DEFAULT_SOURCE
