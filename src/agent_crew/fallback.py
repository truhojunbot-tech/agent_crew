"""Legacy fallback-chain configuration and role defaults.

Provider substitution is disabled by the current routing policy. These
helpers remain for reading existing configuration and resolving role defaults.
"""
import json
import os
from typing import Any, Optional

from agent_crew.role_mapping import (
    KNOWN_ROLE_AGENTS, PANE_PROVIDERS, RoleResolutionError,
    refuse_gemini_outside_athena,
)

DEFAULT_CHAINS: dict[str, list[str]] = {
    "implement": ["claude", "codex"],
    "review":    ["codex", "claude"],
    "test":      ["script"],
}


def _state_project(state_path: str) -> str:
    """The project ``state.json`` declares, else its directory (#924b)."""
    try:
        with open(state_path) as f:
            declared = json.load(f).get("project")
    except Exception:
        declared = None
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    return os.path.basename(os.path.dirname(os.path.abspath(state_path)))


def load_fallback_chains(state_path: Optional[str]) -> dict[str, list[str]]:
    """Read the per-project fallback chain override and merge over defaults.

    Override file lives next to ``state.json`` at ``fallback_chains.json``.
    An absent override uses defaults. An unreadable or malformed declared
    override refuses dispatch through the shared role-resolution error.
    Gemini-family agents outside Athena raise gemini_athena_only (#924b).
    """
    chains: dict[str, list[str]] = {k: list(v) for k, v in DEFAULT_CHAINS.items()}
    if not state_path:
        return chains
    override_path = os.path.join(os.path.dirname(state_path), "fallback_chains.json")
    try:
        with open(override_path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return chains
    except (OSError, ValueError) as exc:
        raise RoleResolutionError("invalid_fallback_chains", override_path) from exc
    if not isinstance(raw, dict):
        raise RoleResolutionError("invalid_fallback_chains", "override must be an object")
    for task_type, agents in raw.items():
        # Check the declared chain before validation or the script-only trim.
        refuse_gemini_outside_athena(_state_project(state_path), agents)
        if (not isinstance(agents, list) or not agents
                or any(not isinstance(a, str) or a not in KNOWN_ROLE_AGENTS for a in agents)):
            raise RoleResolutionError("invalid_fallback_chains", f"invalid chain for {task_type!r}")
        cleaned = list(agents)
        if task_type == "test":
            cleaned = [a for a in cleaned if a == "script"]
        if not cleaned:
            raise RoleResolutionError("invalid_fallback_chains", f"no allowed agents for {task_type!r}")
        chains[task_type] = cleaned
    return chains


def next_agent(
    task_type: str,
    current_agent: Optional[str],
    excluded: Optional[list[str]],
    chains: Optional[dict[str, list[str]]] = None,
    *,
    project: Optional[str] = None,
) -> Optional[str]:
    """Pick the next agent in the fallback chain.

    Strategy:
    1. If ``current_agent`` is in the chain, the next candidate is whatever
       comes after it.
    2. Otherwise (current agent is unknown or already past the chain), start
       from the head.
    3. Skip anything in ``excluded``. Returns ``None`` when the chain is
       exhausted.

    ``project`` is the server's own identity. A chain naming a gemini-family
    agent outside Athena is refused whole, test chains included (#924b).
    """
    chain = (chains or DEFAULT_CHAINS).get(task_type, [])
    refuse_gemini_outside_athena(project, chain)
    excluded_set = set(excluded or [])
    if current_agent and current_agent in excluded_set:
        pass  # current agent already excluded; just walk from start
    if current_agent and current_agent in chain:
        idx = chain.index(current_agent)
        candidates = chain[idx + 1:]
    else:
        candidates = chain
    for agent in candidates:
        if task_type == "test" and agent != "script":
            continue
        if agent not in excluded_set:
            return agent
    return None


def default_agent_for_role(role: str, pane_map: dict[str, Any]) -> Optional[str]:
    """Reverse-lookup the canonical agent name (claude/codex/gemini) for a role.

    `pane_map` carries both role keys and agent keys mapped to pane_ids.
    The agent that shares the same pane as the role is the default for it.
    """
    if not pane_map:
        return None
    role_pane = pane_map.get(role)
    if not role_pane:
        return None
    for k, v in pane_map.items():
        if v == role_pane and k in PANE_PROVIDERS:
            return k
    return None
