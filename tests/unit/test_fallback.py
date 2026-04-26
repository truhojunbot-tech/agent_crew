"""Unit tests for rate-limit auto-fallback (Issue #81)."""
import json

from agent_crew.fallback import (
    DEFAULT_CHAINS,
    default_agent_for_role,
    has_rate_limit_signal,
    is_rate_limit_error,
    load_fallback_chains,
    next_agent,
)


# ---------------------------------------------------------------------------
# is_rate_limit_error
# ---------------------------------------------------------------------------


class TestIsRateLimitError:
    def test_claude_rate_limit(self):
        assert is_rate_limit_error("Claude 5-hour limit reached. Try again later.") is True
        assert is_rate_limit_error("Anthropic API rate-limit exceeded") is True

    def test_codex_usage_limit(self):
        assert is_rate_limit_error("usage limit on the gpt-5 family") is True

    def test_gemini_quota(self):
        assert is_rate_limit_error("Quota exceeded for project 12345") is True
        assert is_rate_limit_error("RESOURCE_EXHAUSTED: requests-per-minute") is True

    def test_generic_429(self):
        assert is_rate_limit_error("HTTP 429 Too Many Requests") is True

    def test_openai_quota(self):
        assert is_rate_limit_error("insufficient_quota: please check your plan") is True

    def test_max_requests(self):
        assert is_rate_limit_error("max requests per day reached") is True

    def test_case_insensitive(self):
        assert is_rate_limit_error("RATE LIMIT") is True
        assert is_rate_limit_error("Usage Limit") is True

    def test_non_rate_limit_failures(self):
        assert is_rate_limit_error("syntax error in test_file.py") is False
        assert is_rate_limit_error("ConnectionError: DNS lookup failed") is False
        assert is_rate_limit_error("Failed to merge PR — conflicts present") is False

    def test_none_and_empty(self):
        assert is_rate_limit_error(None) is False
        assert is_rate_limit_error("") is False


class TestHasRateLimitSignal:
    def test_summary_only(self):
        assert has_rate_limit_signal("Hit usage limit", []) is True

    def test_findings_string(self):
        assert has_rate_limit_signal("ok", ["build failed", "rate limit hit"]) is True

    def test_findings_dict_value(self):
        assert (
            has_rate_limit_signal(
                "ok",
                [{"layer": "build", "message": "quota exceeded"}],
            )
            is True
        )

    def test_no_signal(self):
        assert (
            has_rate_limit_signal(
                "tests passed but lint failed",
                [{"layer": "lint", "message": "unused import"}],
            )
            is False
        )

    def test_empty_inputs(self):
        assert has_rate_limit_signal(None, None) is False
        assert has_rate_limit_signal("", []) is False


# ---------------------------------------------------------------------------
# load_fallback_chains
# ---------------------------------------------------------------------------


class TestLoadFallbackChains:
    def test_no_state_returns_defaults(self):
        chains = load_fallback_chains(None)
        assert chains == DEFAULT_CHAINS
        # Returned dict must be a copy — mutation should not affect defaults
        chains["implement"].append("rogue")
        assert "rogue" not in DEFAULT_CHAINS["implement"]

    def test_missing_override_returns_defaults(self, tmp_path):
        state_path = str(tmp_path / "state.json")
        chains = load_fallback_chains(state_path)
        assert chains == DEFAULT_CHAINS

    def test_override_merges_over_defaults(self, tmp_path):
        state_path = tmp_path / "state.json"
        state_path.write_text("{}")
        override_path = tmp_path / "fallback_chains.json"
        override_path.write_text(
            json.dumps({"implement": ["codex", "claude"]})
        )
        chains = load_fallback_chains(str(state_path))
        assert chains["implement"] == ["codex", "claude"]
        # Other task_types fall back to defaults
        assert chains["review"] == DEFAULT_CHAINS["review"]
        assert chains["test"] == DEFAULT_CHAINS["test"]

    def test_malformed_override_returns_defaults(self, tmp_path):
        state_path = tmp_path / "state.json"
        state_path.write_text("{}")
        override_path = tmp_path / "fallback_chains.json"
        override_path.write_text("not json at all")
        assert load_fallback_chains(str(state_path)) == DEFAULT_CHAINS

    def test_override_with_non_list_value_ignored(self, tmp_path):
        state_path = tmp_path / "state.json"
        state_path.write_text("{}")
        override_path = tmp_path / "fallback_chains.json"
        override_path.write_text(json.dumps({"implement": "claude"}))
        # Non-list value silently ignored
        chains = load_fallback_chains(str(state_path))
        assert chains["implement"] == DEFAULT_CHAINS["implement"]

    def test_override_filters_non_string_entries(self, tmp_path):
        state_path = tmp_path / "state.json"
        state_path.write_text("{}")
        override_path = tmp_path / "fallback_chains.json"
        override_path.write_text(
            json.dumps({"implement": ["codex", None, 1, "claude"]})
        )
        assert load_fallback_chains(str(state_path))["implement"] == ["codex", "claude"]


# ---------------------------------------------------------------------------
# next_agent
# ---------------------------------------------------------------------------


class TestNextAgent:
    def test_progresses_through_chain(self):
        assert next_agent("implement", "claude", []) == "codex"
        assert next_agent("implement", "codex", []) == "gemini"

    def test_chain_exhausted_returns_none(self):
        assert next_agent("implement", "gemini", []) is None

    def test_excluded_agents_skipped(self):
        assert next_agent("implement", "claude", ["codex"]) == "gemini"

    def test_unknown_current_starts_from_head(self):
        assert next_agent("implement", None, []) == "claude"
        assert next_agent("implement", "stranger", []) == "claude"

    def test_unknown_task_type(self):
        assert next_agent("nonsense", "claude", []) is None

    def test_custom_chains(self):
        chains = {"implement": ["alice", "bob", "carol"]}
        assert next_agent("implement", "alice", [], chains) == "bob"
        assert next_agent("implement", "bob", [], chains) == "carol"
        assert next_agent("implement", "carol", [], chains) is None

    def test_excluded_includes_current(self):
        # current agent is already in excluded — should still walk forward
        assert next_agent("implement", "claude", ["claude"]) == "codex"


# ---------------------------------------------------------------------------
# default_agent_for_role
# ---------------------------------------------------------------------------


class TestDefaultAgentForRole:
    def test_resolves_implementer_to_claude(self):
        pane_map = {
            "implementer": "%100",
            "claude": "%100",
            "reviewer": "%101",
            "codex": "%101",
        }
        assert default_agent_for_role("implementer", pane_map) == "claude"
        assert default_agent_for_role("reviewer", pane_map) == "codex"

    def test_unknown_role_returns_none(self):
        pane_map = {"implementer": "%100", "claude": "%100"}
        assert default_agent_for_role("unknown", pane_map) is None

    def test_role_with_no_agent_partner_returns_none(self):
        pane_map = {"implementer": "%999"}  # no agent maps to this pane
        assert default_agent_for_role("implementer", pane_map) is None

    def test_empty_pane_map(self):
        assert default_agent_for_role("implementer", {}) is None
