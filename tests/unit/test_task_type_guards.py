"""Task-description guards for review/test tasks (Issue #110 phase 4-b).

Layered safeguard so a project's developer-facing GEMINI.md/AGENTS.md
can't make the tester/reviewer agent modify code on a task it should
just be verifying. Two layers:

1. ``server._guard_description`` prepends a hard-coded `[VERIFY ONLY ...]`
   or `[REVIEW ONLY ...]` block to the task description before it goes
   over either delivery path.
2. The MCP path (`mcp_server.get_next_task`) routes through the same
   guard, so the description the agent's LLM sees is identical
   regardless of whether delivery was via tmux push or MCP pull.

These tests pin both paths so a future refactor can't accidentally
drop one.
"""
from agent_crew.protocol import TaskRequest


def _make_task(task_id="t1", task_type="implement", description="do work"):
    return TaskRequest(
        task_id=task_id,
        task_type=task_type,
        description=description,
        branch="main",
        priority=3,
        context={},
    )


# ---------------------------------------------------------------------------
# server._guard_description: pure prepend logic
# ---------------------------------------------------------------------------


class TestGuardDescription:
    def test_implement_passes_through_unchanged(self):
        from agent_crew.server import _guard_description
        task = _make_task("t-i", task_type="implement", description="add feature X")
        assert _guard_description(task) == "add feature X"

    def test_discuss_passes_through_unchanged(self):
        from agent_crew.server import _guard_description
        task = _make_task("t-d", task_type="discuss", description="debate Y")
        assert _guard_description(task) == "debate Y"

    def test_review_gets_review_only_prefix(self):
        from agent_crew.server import _guard_description
        task = _make_task("t-r", task_type="review", description="review PR #42")
        out = _guard_description(task)
        assert out.startswith("[REVIEW ONLY")
        assert "review PR #42" in out
        assert "do NOT modify" in out
        assert "do NOT" in out  # all-caps directive

    def test_test_gets_verify_only_prefix(self):
        from agent_crew.server import _guard_description
        task = _make_task("t-t", task_type="test", description="run tests for branch X")
        out = _guard_description(task)
        assert out.startswith("[VERIFY ONLY")
        assert "run tests for branch X" in out
        assert "do NOT modify" in out
        assert "do NOT push" in out
        assert "PR" in out  # forbids opening/force-pushing PR

    def test_idempotent_on_already_prefixed_description(self):
        from agent_crew.server import _guard_description
        task = _make_task("t-r", task_type="review", description="review PR #42")
        once = _guard_description(task)
        # Second call shouldn't double-prefix.
        task2 = _make_task("t-r", task_type="review", description=once)
        twice = _guard_description(task2)
        # The result should not contain the guard string twice.
        assert twice.count("[REVIEW ONLY") == 1


# ---------------------------------------------------------------------------
# Push path (tmux-paste-buffer message) carries the guard
# ---------------------------------------------------------------------------


class TestFormatTaskMessage:
    def test_review_task_message_has_guard_in_description(self):
        from agent_crew.server import _format_task_message
        task = _make_task("t-r", task_type="review", description="review PR #42")
        msg = _format_task_message(task, port=8200)
        # Find the description line and confirm guard is there.
        assert "[REVIEW ONLY" in msg
        assert "review PR #42" in msg

    def test_test_task_message_has_guard_in_description(self):
        from agent_crew.server import _format_task_message
        task = _make_task("t-t", task_type="test", description="run suite")
        msg = _format_task_message(task, port=8200)
        assert "[VERIFY ONLY" in msg

    def test_implement_task_message_has_no_guard(self):
        from agent_crew.server import _format_task_message
        task = _make_task("t-i", task_type="implement",
                          description="add feature X")
        msg = _format_task_message(task, port=8200)
        assert "VERIFY ONLY" not in msg
        assert "REVIEW ONLY" not in msg


# ---------------------------------------------------------------------------
# MCP path (`get_next_task` return value) carries the same guard
# ---------------------------------------------------------------------------


class TestMcpPathGuard:
    def test_get_next_task_returns_guarded_review_description(self, tmp_db):
        import asyncio

        from agent_crew.mcp_server import build_mcp_server
        from agent_crew.queue import TaskQueue

        TaskQueue(tmp_db).enqueue(_make_task("mcp-r", task_type="review",
                                             description="review PR #99"))
        mcp = build_mcp_server(tmp_db)
        tool = mcp._tool_manager._tools["get_next_task"]
        func = tool.fn
        if asyncio.iscoroutinefunction(func):
            result = asyncio.run(func(agent="codex"))
        else:
            result = func(agent="codex")
        assert result is not None
        assert result["description"].startswith("[REVIEW ONLY")
        assert "review PR #99" in result["description"]

    def test_get_next_task_returns_guarded_test_description(self, tmp_db):
        import asyncio

        from agent_crew.mcp_server import build_mcp_server
        from agent_crew.queue import TaskQueue

        TaskQueue(tmp_db).enqueue(_make_task("mcp-t", task_type="test",
                                             description="verify branch X"))
        mcp = build_mcp_server(tmp_db)
        tool = mcp._tool_manager._tools["get_next_task"]
        func = tool.fn
        if asyncio.iscoroutinefunction(func):
            result = asyncio.run(func(agent="gemini"))
        else:
            result = func(agent="gemini")
        assert result is not None
        assert result["description"].startswith("[VERIFY ONLY")
        assert "verify branch X" in result["description"]

    def test_get_next_task_implement_no_guard(self, tmp_db):
        import asyncio

        from agent_crew.mcp_server import build_mcp_server
        from agent_crew.queue import TaskQueue

        TaskQueue(tmp_db).enqueue(_make_task("mcp-i", task_type="implement",
                                             description="impl feature Y"))
        mcp = build_mcp_server(tmp_db)
        tool = mcp._tool_manager._tools["get_next_task"]
        func = tool.fn
        if asyncio.iscoroutinefunction(func):
            result = asyncio.run(func(agent="claude"))
        else:
            result = func(agent="claude")
        assert result is not None
        assert result["description"] == "impl feature Y"
