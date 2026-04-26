"""Unit tests for the task-loop system prompt builders (Issue #106)."""
from agent_crew.prompts.task_loop import (
    build_task_loop_prompt,
    build_task_loop_prompt_compact,
)


class TestFullPrompt:
    def test_includes_agent_name(self):
        p = build_task_loop_prompt("claude", role="implementer")
        assert "claude" in p
        assert "[agent: claude]" in p

    def test_lists_all_task_types_for_dynamic_reassignment(self):
        """Phase 3 (#106) — agents are no longer locked into a single
        task_type. Every prompt must enumerate all four so the agent
        knows how to dispatch when an override sends a non-default
        type their way."""
        for agent, role in [("claude", "implementer"), ("codex", "reviewer"),
                            ("gemini", "tester")]:
            p = build_task_loop_prompt(agent, role=role)
            for task_type in ("implement", "review", "test", "discuss"):
                assert task_type in p
            # Default role still surfaces in the call example.
            assert role in p

    def test_unknown_role_falls_back_to_implementer(self):
        p = build_task_loop_prompt("claude", role="weird")
        # Falls back to implement-only listing.
        assert "implement" in p

    def test_mentions_each_mcp_tool(self):
        p = build_task_loop_prompt("claude", role="implementer")
        for tool in ("get_next_task", "submit_result", "bump_activity",
                     "get_task"):
            assert tool in p

    def test_calls_out_loop_explicitly(self):
        p = build_task_loop_prompt("claude")
        # The whole point of #106 — the agent's LLM must see "Loop" / "Continuously"
        # as a directive.
        assert "Continuously" in p or "Loop" in p
        assert "Return to step 1" in p

    def test_review_task_pins_to_pr_head(self):
        p = build_task_loop_prompt("codex", role="reviewer")
        # Locks in the #86 freshness rule.
        assert "gh pr diff" in p
        assert "do NOT" in p.lower() or "do not" in p.lower()

    def test_needs_human_escape_hatch_documented(self):
        p = build_task_loop_prompt("claude")
        assert "needs_human" in p

    def test_precedence_block_authoritative_for_role_boundaries(self):
        """Issue #110 phase 4-b — the precedence block must say agent_crew
        wins on role/loop conflicts, and project content applies only to
        style/conventions inside the authorized work."""
        p = build_task_loop_prompt("gemini", role="tester")
        assert "PRECEDENCE" in p
        assert "authoritative" in p.lower() or "wins" in p.lower()

    def test_precedence_says_task_type_drives_behavior(self):
        """Dynamic role reassignment (#106 phase 3 + #81) means an agent's
        identity is NOT a reliable proxy for what work it does on a given
        task. The precedence block must call this out so a project's
        gemini-as-developer guide doesn't make gemini-as-tester modify code."""
        p = build_task_loop_prompt("gemini", role="tester")
        # The exact wording can change; pin the substantive contract.
        assert "task_type" in p
        # Each task_type's allowed action surface is enumerated so the
        # agent's LLM can dispatch correctly.
        for tt in ("implement", "review", "test", "discuss"):
            assert tt in p
        # And the no-modify-on-test/review rules are explicit.
        assert "do NOT modify" in p or "no commit" in p.lower()


class TestCompactPrompt:
    def test_short_enough_to_survive_compact(self):
        p = build_task_loop_prompt_compact("claude")
        # Eyeball: PS used ~5 short sentences. Guard against accidentally
        # blowing this up to a multi-page prompt.
        assert len(p) < 800

    def test_lists_loop_steps(self):
        p = build_task_loop_prompt_compact("claude")
        assert "get_next_task" in p
        assert "submit_result" in p
        assert "Loop" in p or "loop" in p

    def test_carries_role(self):
        p = build_task_loop_prompt_compact("codex", role="reviewer")
        assert "reviewer" in p
        assert "codex" in p

    def test_default_role_is_implementer(self):
        p = build_task_loop_prompt_compact("claude")
        assert "implementer" in p
