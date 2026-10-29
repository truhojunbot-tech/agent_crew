"""Legacy fallback-chain configuration and role defaults.

Provider substitution is disabled by the current routing policy. These
helpers remain for reading existing configuration and resolving role defaults.
"""
import json
import os
from typing import Any, Optional

DEFAULT_CHAINS: dict[str, list[str]] = {
    "implement": ["claude", "codex"],
    "review":    ["codex", "claude"],
    "test":      ["script"],
}


def load_fallback_chains(state_path: Optional[str]) -> dict[str, list[str]]:
    """Read the per-project fallback chain override and merge over defaults.

    Override file lives next to ``state.json`` at ``fallback_chains.json``.
    Missing file or malformed JSON quietly returns the defaults; we never
    want a config typo to disable the fallback policy.
    """
    chains: dict[str, list[str]] = {k: list(v) for k, v in DEFAULT_CHAINS.items()}
    if not state_path:
        return chains
    override_path = os.path.join(os.path.dirname(state_path), "fallback_chains.json")
    if not os.path.isfile(override_path):
        return chains
    try:
        with open(override_path) as f:
            raw = json.load(f)
    except Exception:
        return chains
    if not isinstance(raw, dict):
        return chains
    for task_type, agents in raw.items():
        if not isinstance(agents, list):
            continue
        cleaned = [a for a in agents if isinstance(a, str) and a]
        if task_type == "test":
            cleaned = [a for a in cleaned if a == "script"]
        if cleaned:
            chains[task_type] = cleaned
    return chains


def next_agent(
    task_type: str,
    current_agent: Optional[str],
    excluded: Optional[list[str]],
    chains: Optional[dict[str, list[str]]] = None,
) -> Optional[str]:
    """Pick the next agent in the fallback chain.

    Strategy:
    1. If ``current_agent`` is in the chain, the next candidate is whatever
       comes after it.
    2. Otherwise (current agent is unknown or already past the chain), start
       from the head.
    3. Skip anything in ``excluded``. Returns ``None`` when the chain is
       exhausted.
    """
    chain = (chains or DEFAULT_CHAINS).get(task_type, [])
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
        if v == role_pane and k in ("claude", "codex", "gemini"):
            return k
    return None
