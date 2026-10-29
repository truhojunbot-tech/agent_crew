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
    # not a provider. The Gemini provider code remains for Athena work.
    "tester": "script",
}
#: Agent-keyed legacy state (``worktrees: {agent: path}``) names a worktree's
#: role by its agent. This is a schema reading, not the default provider: a
#: A legacy gemini worktree can still be read as a tester worktree; the
#: effective role mapping refuses that provider for general test work (#924).
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
    "tester": ("script",),
}
TASK_TYPE_TO_ROLE = {"implement": "implementer", "review": "reviewer", "test": "tester"}
#: The pull routes also accept ``coder`` (an implementer alias) and ``panel``
#: (discussion work, which no provider family is barred from). Without them an
#: anonymous discussion pull was refused as if it were restricted work.
_PULL_ROLE_POLICY: dict[str, tuple[str, ...] | None] = {
    **ROLE_PROVIDER_POLICY,
    "coder": ROLE_PROVIDER_POLICY["implementer"],
    "panel": None,
}


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


def anonymous_pull_refusal(role: object, agent: object) -> str | None:
    """Reason a pull by ``agent`` must be refused before it claims (#889).

    A blank agent has no provider family, so :func:`role_policy_refusal` cannot
    judge it. On a pull route that is a bypass: an unnamed caller would receive
    restricted work. Only a role with no family restriction may be pulled
    anonymously; a blank role could match any task type, so it is refused too.
    """
    if str(agent or "").strip():
        return None
    role = role.strip() if isinstance(role, str) else ""
    if role and role in _PULL_ROLE_POLICY and _PULL_ROLE_POLICY[role] is None:
        return None
    return (f"role_policy: an unnamed agent may not pull role {role or '(any)'!r}; "
            "name the provider so the role policy can check its family")


#: Role-mapped backends with no checkout of their own. A task bound to one is
#: dispatchable only while that backend still holds the task's role (#891).
ROLE_BOUND_PROVIDERS = ("claude_cloud",)


def provider_task_types(provider: str, role_to_agent: Mapping[str, str]) -> tuple[str, ...]:
    """Task types ``provider`` holds under the CURRENT role map (#891).

    A binding stored on a task (``agent_override``) is only as current as the
    role map it was written under; claim and dispatch re-read the map here.
    """
    return tuple(sorted(task_type for task_type, role in TASK_TYPE_TO_ROLE.items()
                        if role_to_agent.get(role) == provider))


def validate_explicit_role_agents(mapping: Mapping[str, object]) -> dict[str, str]:
    """Validate a complete explicit mapping and its tester provider policy."""
    if set(mapping) != set(ROLE_ORDER):
        raise ValueError("role_agents must set implementer, reviewer, and tester exactly once")
    result = {}
    for role in ROLE_ORDER:
        agent = mapping[role]
        if not isinstance(agent, str) or not agent.strip():
            raise ValueError(f"agent for {role!r} must be a non-empty string")
        if role == "tester" and role_policy_refusal(role, agent):
            raise ValueError(role_policy_refusal(role, agent))
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
            normalized = dict(explicit)
            tester = normalized.get("tester")
            refusal = role_policy_refusal("tester", tester)
            if refusal:
                normalized["tester"] = DEFAULT_ROLE_TO_AGENT["tester"]
            mapping = validate_explicit_role_agents(normalized)
            if refusal:
                logger.warning(
                    "project %s has refused explicit tester %r (%s); using script tester",
                    project, tester, refusal,
                )
            return mapping, EXPLICIT_SOURCE
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
            if (role in ROLE_ORDER and isinstance(agent, str) and agent.strip()
                    and role_policy_refusal(role, agent) is None):
                result[role] = agent.strip()
                found = True
        if found:
            return result, LEGACY_SOURCE
    return result, DEFAULT_SOURCE
