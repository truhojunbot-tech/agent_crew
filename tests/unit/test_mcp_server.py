"""Parity tests for the MCP server (Issue #106 PoC).

Each test enqueues a task via the existing `TaskQueue`, drives the MCP
tool function, and asserts the queue ends up in the same state the HTTP
path would produce. We invoke the underlying tool callables directly via
FastMCP's `_tool_manager` registry — that's the same path the MCP runtime
uses, just without the JSON-RPC envelope.
"""
import asyncio

from agent_crew.mcp_server import build_mcp_server
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


def _call_tool(mcp, tool_name: str, **kwargs):
    """Invoke a registered MCP tool. FastMCP exposes tools via an internal
    registry; we look up the underlying Python function and call it."""
    tool = mcp._tool_manager._tools[tool_name]
    func = tool.fn
    if asyncio.iscoroutinefunction(func):
        return asyncio.run(func(**kwargs))
    return func(**kwargs)


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
# Server construction
# ---------------------------------------------------------------------------


class TestBuild:
    def test_returns_fastmcp_with_attached_queue(self, tmp_db):
        mcp = build_mcp_server(tmp_db)
        # Internal: the queue is stashed on the server for tests / introspection.
        assert mcp._agent_crew_queue is not None
        # Confirm it's pointed at our temp DB by enqueueing through it.
        mcp._agent_crew_queue.enqueue(_make_task("t-attach"))
        assert any(t.task_id == "t-attach" for t in TaskQueue(tmp_db).list_tasks())


# ---------------------------------------------------------------------------
# get_next_task / get_next_discuss_task
# ---------------------------------------------------------------------------


class TestGetNextTask:
    def test_returns_dict_for_pending_task(self, tmp_db):
        TaskQueue(tmp_db).enqueue(_make_task("t-impl"))
        mcp = build_mcp_server(tmp_db)
        result = _call_tool(mcp, "get_next_task", role="implementer")
        assert result is not None
        assert result["task_id"] == "t-impl"
        assert result["task_type"] == "implement"
        # And the queue side: task is now in_progress.
        assert mcp._agent_crew_queue.has_in_progress("implement")

    def test_empty_queue_returns_none(self, tmp_db):
        mcp = build_mcp_server(tmp_db)
        assert _call_tool(mcp, "get_next_task", role="implementer") is None

    def test_role_filter_skips_other_types(self, tmp_db):
        TaskQueue(tmp_db).enqueue(_make_task("t-rev", task_type="review"))
        mcp = build_mcp_server(tmp_db)
        # Asking for an implementer task should not surface the review one.
        assert _call_tool(mcp, "get_next_task", role="implementer") is None
        # But a reviewer call sees it.
        result = _call_tool(mcp, "get_next_task", role="reviewer")
        assert result is not None
        assert result["task_id"] == "t-rev"

    def test_discuss_path(self, tmp_db):
        # discuss tasks use a separate dequeue scoped on the agent, not role.
        q = TaskQueue(tmp_db)
        task = _make_task("d-claude", task_type="discuss")
        task.context = {"agent": "claude"}
        q.enqueue(task)
        mcp = build_mcp_server(tmp_db)
        result = _call_tool(mcp, "get_next_discuss_task", agent="claude")
        assert result is not None
        assert result["task_id"] == "d-claude"


# ---------------------------------------------------------------------------
# submit_result
# ---------------------------------------------------------------------------


class TestSubmitResult:
    def test_completed_path_returns_ack(self, tmp_db):
        TaskQueue(tmp_db).enqueue(_make_task("t-ok"))
        mcp = build_mcp_server(tmp_db)
        _call_tool(mcp, "get_next_task", role="implementer")  # transition to in_progress
        ack = _call_tool(mcp, "submit_result", task_id="t-ok",
                         status="completed", summary="done")
        assert ack["acknowledged"] is True
        assert ack["task_type"] == "implement"

    def test_failed_status_carries_summary(self, tmp_db):
        TaskQueue(tmp_db).enqueue(_make_task("t-fail"))
        mcp = build_mcp_server(tmp_db)
        _call_tool(mcp, "get_next_task", role="implementer")
        ack = _call_tool(mcp, "submit_result", task_id="t-fail",
                         status="failed", summary="boom")
        assert ack["acknowledged"] is True

    def test_invalid_status_returns_error_not_raise(self, tmp_db):
        TaskQueue(tmp_db).enqueue(_make_task("t-bad"))
        mcp = build_mcp_server(tmp_db)
        _call_tool(mcp, "get_next_task", role="implementer")
        ack = _call_tool(mcp, "submit_result", task_id="t-bad",
                         status="weird", summary="?")
        assert ack["acknowledged"] is False
        assert "error" in ack

    def test_unknown_task_returns_error(self, tmp_db):
        mcp = build_mcp_server(tmp_db)
        ack = _call_tool(mcp, "submit_result", task_id="ghost",
                         status="completed", summary="-")
        assert ack["acknowledged"] is False
        assert "error" in ack

    def test_review_findings_propagate_to_queue(self, tmp_db):
        TaskQueue(tmp_db).enqueue(_make_task("t-rev", task_type="review"))
        mcp = build_mcp_server(tmp_db)
        _call_tool(mcp, "get_next_task", role="reviewer")
        ack = _call_tool(
            mcp,
            "submit_result",
            task_id="t-rev",
            status="completed",
            summary="reviewed",
            verdict="request_changes",
            findings=["[bug] off-by-one in step 2"],
        )
        assert ack["acknowledged"] is True


# ---------------------------------------------------------------------------
# bump_activity / get_task / list_pending / cancel_task
# ---------------------------------------------------------------------------


class TestAuxTools:
    def test_bump_activity(self, tmp_db):
        TaskQueue(tmp_db).enqueue(_make_task("t-bump"))
        mcp = build_mcp_server(tmp_db)
        _call_tool(mcp, "get_next_task", role="implementer")
        ack = _call_tool(mcp, "bump_activity", task_id="t-bump")
        assert ack["acknowledged"] is True

    def test_get_task_returns_dict(self, tmp_db):
        TaskQueue(tmp_db).enqueue(_make_task("t-look"))
        mcp = build_mcp_server(tmp_db)
        result = _call_tool(mcp, "get_task", task_id="t-look")
        assert result is not None
        assert result["task_id"] == "t-look"

    def test_get_task_unknown_returns_none(self, tmp_db):
        mcp = build_mcp_server(tmp_db)
        assert _call_tool(mcp, "get_task", task_id="ghost") is None

    def test_list_pending_filters_by_role(self, tmp_db):
        q = TaskQueue(tmp_db)
        q.enqueue(_make_task("t-i1", task_type="implement"))
        q.enqueue(_make_task("t-i2", task_type="implement"))
        q.enqueue(_make_task("t-r", task_type="review"))
        mcp = build_mcp_server(tmp_db)
        impl = _call_tool(mcp, "list_pending", role="implementer")
        assert {x["task_id"] for x in impl} == {"t-i1", "t-i2"}
        rev = _call_tool(mcp, "list_pending", role="reviewer")
        assert [x["task_id"] for x in rev] == ["t-r"]

    def test_list_pending_unknown_role_returns_empty(self, tmp_db):
        TaskQueue(tmp_db).enqueue(_make_task())
        mcp = build_mcp_server(tmp_db)
        assert _call_tool(mcp, "list_pending", role="bogus") == []

    def test_cancel_task_idempotent(self, tmp_db):
        TaskQueue(tmp_db).enqueue(_make_task("t-cancel"))
        mcp = build_mcp_server(tmp_db)
        ack = _call_tool(mcp, "cancel_task", task_id="t-cancel")
        assert ack["acknowledged"] is True
        # Cancelling again must not raise.
        ack2 = _call_tool(mcp, "cancel_task", task_id="t-cancel")
        assert ack2["acknowledged"] is True
