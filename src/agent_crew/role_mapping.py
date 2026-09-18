"""Side-effect-free project role mapping defaults and resolution."""

DEFAULT_ROLE_TO_AGENT = {
    "implementer": "claude",
    "reviewer": "codex",
    "tester": "gemini",
}
DEFAULT_AGENT_TO_ROLE = {agent: role for role, agent in DEFAULT_ROLE_TO_AGENT.items()}


def resolve_role_to_agent(state: dict | None) -> tuple[dict[str, str], str]:
    """Resolve role assignments from explicit, legacy, or default state."""
    if not state:
        return dict(DEFAULT_ROLE_TO_AGENT), "hardcoded default"

    explicit_mapping = state.get("explicit_role_to_agent")
    if isinstance(explicit_mapping, dict):
        return dict(explicit_mapping), "explicit project config"

    roles_list = state.get("roles")
    if roles_list:
        result = dict(DEFAULT_ROLE_TO_AGENT)
        for role_config in roles_list:
            try:
                role = role_config.get("role")
                agent = role_config.get("agent")
                if role and agent:
                    result[role] = agent
            except Exception:
                # Match the old loader's outer exception handler: retain the
                # valid prefix and do not process any later legacy entries.
                return result, "legacy state.json.roles"
        return result, "legacy state.json.roles"

    return dict(DEFAULT_ROLE_TO_AGENT), "hardcoded default"
