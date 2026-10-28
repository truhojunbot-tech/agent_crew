"""Provider-neutral, durable project role assignment (#337)."""

from __future__ import annotations

import logging
from collections.abc import Mapping


logger = logging.getLogger(__name__)

ROLE_ORDER = ("implementer", "reviewer", "tester")
DEFAULT_ROLE_TO_AGENT = {
    "implementer": "codex",
    "reviewer": "claude",
    # #772: the test stage is a deterministic script run (script_tester.py),
    # not a provider. The gemini provider code stays; an explicit
    # ``role_agents.tester = "gemini"`` restores the LLM tester.
    "tester": "script",
}
#: Agent-keyed legacy state (``worktrees: {agent: path}``) names a worktree's
#: role by its agent. This is a schema reading, not the default provider: a
#: gemini worktree is still the tester's checkout even though the default
#: tester is the script (#772), and an override to gemini still finds it.
LEGACY_AGENT_TO_ROLE = {"codex": "implementer", "claude": "reviewer", "gemini": "tester"}
EXPLICIT_SOURCE = "explicit project config"
LEGACY_SOURCE = "legacy state.json.roles"
DEFAULT_SOURCE = "hardcoded default"


def provider_family(agent: object) -> str:
    """Model family of an agent name, for review-independence checks (#871).

    ``claude_cloud`` is the same model as ``claude``; comparing raw strings let
    a claude-implemented PR pass a claude_cloud review.
    """
    name = str(agent or "").strip().lower()
    if name == "claude" or name.startswith(("claude-", "claude_")):
        return "claude"
    for family in ("codex", "gemini"):
        if name.startswith(family):
            return family
    if name == "agy" or name.startswith(("agy-", "agy_")):
        return "gemini"  # the gemini tester binary (#236)
    return name


#: Which provider families may hold each role (#889). The one policy every
#: route reads: explicit override, ``implementer_agent``, role assignment, fix
#: rounds, retries. ``None`` means no family restriction — review independence
#: is the separate #871 rule. ``claude_cloud`` is in the ``claude`` family.
ROLE_PROVIDER_POLICY: dict[str, tuple[str, ...] | None] = {
    "implementer": ("claude", "codex"),
    "reviewer": None,
    "tester": ("gemini", "script"),
}
TASK_TYPE_TO_ROLE = {"implement": "implementer", "review": "reviewer", "test": "tester"}


def role_policy() -> dict[str, list[str] | None]:
    """JSON view of :data:`ROLE_PROVIDER_POLICY` for enqueue-side pre-checks."""
    return {role: (list(families) if families is not None else None)
            for role, families in ROLE_PROVIDER_POLICY.items()}


def role_policy_refusal(role: object, agent: object) -> str | None:
    """Reason ``agent`` may not hold ``role``, or ``None`` when allowed."""
    allowed = ROLE_PROVIDER_POLICY.get(role) if isinstance(role, str) else None
    if allowed is None or not str(agent or "").strip():
        return None
    if provider_family(agent) in allowed:
        return None
    return (f"role_policy: {str(agent).strip()!r} (family "
            f"{provider_family(agent)!r}) may not hold role {role!r}; "
            f"allowed families: {list(allowed)}")


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
