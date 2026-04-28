"""HTTP ↔ MCP parity tests (Issue #120, acceptance for #106).

Each agent-facing HTTP endpoint must have an MCP tool that operates on the
same SQLite queue with semantically identical behavior. We pair them off
one by one — drive each side against an isolated DB, then compare the
returned payload and the post-call queue state.

Pairs covered:

    GET    /tasks/next?role=...   ↔ get_next_task(role=...)
    POST   /tasks/{id}/result     ↔ submit_result(...)
    GET    /tasks/{id}            ↔ get_task(task_id=...)
    GET    /tasks?status=pending  ↔ list_pending(role="")
    DELETE /tasks/{id}            ↔ cancel_task(task_id=...)

Out of parity scope (intentional):

    bump_activity  — MCP-only; no HTTP equivalent ships today
    /gates/*       — operator/escalation API, never called by an agent
    /tasks/{id}/checkpoint(s) — debugging aid, not in the task-loop prompt
    POST /tasks    — producer-facing (CLI, n8n, webhooks); agents never enqueue

If a future change makes one of those agent-facing, add it here.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from agent_crew.mcp_server import build_mcp_server
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call_tool(mcp, tool_name: str, **kwargs):
    """Invoke a registered MCP tool. FastMCP exposes tools via an internal
    registry; we look up the underlying Python function and call it
    directly — same code path the JSON-RPC envelope would hit."""
    tool = mcp._tool_manager._tools[tool_name]
    func = tool.fn
    if asyncio.iscoroutinefunction(func):
        return asyncio.run(func(**kwargs))
    return func(**kwargs)


def _enqueue_task(
    db_path: str,
    task_id: str,
    task_type: str = "implement",
    description: str = "do work",
    priority: int = 3,
    branch: str = "main",
    context: dict | None = None,
) -> None:
    """Seed a task on the shared DB without going through HTTP/MCP — keeps
    each parity test starting from an identical, neutral state."""
    from agent_crew.protocol import TaskRequest

    TaskQueue(db_path).enqueue(
        TaskRequest(
            task_id=task_id,
            task_type=task_type,  # type: ignore[arg-type]
            description=description,
            branch=branch,
            priority=priority,
            context=context or {},
        )
    )


def _queue_snapshot(db_path: str) -> dict[str, dict]:
    """Capture {task_id: {status, ...}} to compare DB state across calls.

    Only fields that both transports influence are kept — any layer above
    SQLite that sets bookkeeping fields independently (timestamps written
    by python at task-creation time) is excluded so equality stays robust.
    """
    q = TaskQueue(db_path)
    snap = {}
    for t in q.list_tasks():
        # ``status`` lives outside TaskRequest — ask the queue.
        rows = q._connect().execute(
            "SELECT status FROM tasks WHERE task_id = ?", (t.task_id,)
        ).fetchall()
        status = rows[0]["status"] if rows else None
        snap[t.task_id] = {
            "task_id": t.task_id,
            "task_type": t.task_type,
            "status": status,
            "context": t.context,
        }
    return snap


def _normalize_task_payload(payload):
    """Strip transport-only differences. Both HTTP (Pydantic) and MCP
    (asdict) emit the same keys for TaskRequest, but JSON serialization
    can change tuple/list shape on context fields."""
    if payload is None:
        return None
    if isinstance(payload, list):
        return [_normalize_task_payload(p) for p in payload]
    keys = ("task_id", "task_type", "description", "branch", "priority", "context")
    return {k: payload.get(k) for k in keys if k in payload}


@pytest.fixture
def parity_pair(tmp_path):
    """Build a fresh HTTP app + MCP server bound to the same SQLite DB.

    Yields a tuple ``(http_client, mcp, db_path)``. The test is responsible
    for seeding tasks via ``_enqueue_task(db_path, ...)``.
    """
    db_path = str(tmp_path / "parity.db")
    app = create_app(db_path=db_path, port=0, watchdog_disabled=True)
    mcp = build_mcp_server(db_path)
    with TestClient(app) as client:
        yield client, mcp, db_path


# ---------------------------------------------------------------------------
# GET /tasks/next  ↔  get_next_task
# ---------------------------------------------------------------------------


class TestGetNextTaskParity:
    def test_returns_same_payload_for_pending_task(self, parity_pair, tmp_path):
        # HTTP side
        http_client, _, db_a = parity_pair
        _enqueue_task(db_a, "p-impl-1", task_type="implement")
        http_resp = http_client.get("/tasks/next", params={"role": "implementer"})
        assert http_resp.status_code == 200
        http_payload = http_resp.json()

        # MCP side — separate DB so the dequeue doesn't double-count.
        db_b = str(tmp_path / "parity_mcp.db")
        _enqueue_task(db_b, "p-impl-1", task_type="implement")
        mcp = build_mcp_server(db_b)
        mcp_payload = _call_tool(mcp, "get_next_task", role="implementer")

        assert _normalize_task_payload(http_payload) == _normalize_task_payload(mcp_payload)

        # Queue state must agree: both sides flipped the task to in_progress.
        assert _queue_snapshot(db_a) == _queue_snapshot(db_b)

    def test_empty_queue_both_return_none_or_null(self, parity_pair, tmp_path):
        http_client, _, _ = parity_pair
        http_resp = http_client.get("/tasks/next", params={"role": "implementer"})
        # FastAPI returns ``null`` (200) when the handler returned None.
        assert http_resp.status_code == 200
        assert http_resp.json() is None

        db_b = str(tmp_path / "parity_mcp.db")
        # Touch the DB so MCP doesn't fail on missing file.
        TaskQueue(db_b)
        mcp = build_mcp_server(db_b)
        assert _call_tool(mcp, "get_next_task", role="implementer") is None

    def test_role_filter_skips_other_types(self, parity_pair, tmp_path):
        http_client, _, db_a = parity_pair
        _enqueue_task(db_a, "p-rev-1", task_type="review")
        http_resp = http_client.get("/tasks/next", params={"role": "implementer"})
        assert http_resp.status_code == 200
        assert http_resp.json() is None

        db_b = str(tmp_path / "parity_mcp.db")
        _enqueue_task(db_b, "p-rev-1", task_type="review")
        mcp = build_mcp_server(db_b)
        assert _call_tool(mcp, "get_next_task", role="implementer") is None


# ---------------------------------------------------------------------------
# POST /tasks/{id}/result  ↔  submit_result
# ---------------------------------------------------------------------------


class TestSubmitResultParity:
    def test_completed_status_marks_original_task_done_both_sides(
        self, parity_pair, tmp_path
    ):
        """The direct effect of submit_result — flipping the task to
        ``completed`` — must agree across transports. Cascading effects
        (auto-enqueue of the next stage) live behind a separate parity
        check that intentionally documents the current divergence."""
        http_client, _, db_a = parity_pair
        _enqueue_task(db_a, "p-sub-1")
        http_client.get("/tasks/next", params={"role": "implementer"})
        result_payload = {
            "task_id": "p-sub-1",
            "status": "completed",
            "summary": "shipped",
            "verdict": None,
            "findings": [],
            "pr_number": 42,
        }
        http_resp = http_client.post("/tasks/p-sub-1/result", json=result_payload)
        assert http_resp.status_code == 200

        db_b = str(tmp_path / "parity_mcp.db")
        _enqueue_task(db_b, "p-sub-1")
        mcp = build_mcp_server(db_b)
        _call_tool(mcp, "get_next_task", role="implementer")
        mcp_resp = _call_tool(
            mcp,
            "submit_result",
            task_id="p-sub-1",
            status="completed",
            summary="shipped",
            verdict=None,
            findings=[],
            pr_number=42,
        )
        assert mcp_resp["acknowledged"] is True

        # Compare only the original task's row — auto-enqueue cascades
        # are tracked separately.
        snap_a = {
            tid: row for tid, row in _queue_snapshot(db_a).items()
            if tid == "p-sub-1"
        }
        snap_b = {
            tid: row for tid, row in _queue_snapshot(db_b).items()
            if tid == "p-sub-1"
        }
        assert snap_a == snap_b

    @pytest.mark.xfail(
        reason=(
            "Known parity gap: HTTP submit_result auto-enqueues the next "
            "stage (impl→review, review→test) via _auto_enqueue_review / "
            "_auto_enqueue_test, which live inside create_app. MCP "
            "submit_result calls queue.submit_result directly and skips "
            "those hooks, so the pipeline stalls after the first stage if "
            "an agent only uses MCP. Must be resolved before #119 cutover. "
            "Tracking: #123."
        ),
        strict=True,
    )
    def test_completed_impl_auto_enqueues_review_on_both_sides(
        self, parity_pair, tmp_path
    ):
        http_client, _, db_a = parity_pair
        _enqueue_task(db_a, "p-cascade-1")
        http_client.get("/tasks/next", params={"role": "implementer"})
        http_client.post(
            "/tasks/p-cascade-1/result",
            json={
                "task_id": "p-cascade-1",
                "status": "completed",
                "summary": "ok",
                "verdict": None,
                "findings": [],
                "pr_number": None,
            },
        )

        db_b = str(tmp_path / "parity_mcp.db")
        _enqueue_task(db_b, "p-cascade-1")
        mcp = build_mcp_server(db_b)
        _call_tool(mcp, "get_next_task", role="implementer")
        _call_tool(
            mcp,
            "submit_result",
            task_id="p-cascade-1",
            status="completed",
        )

        http_review_count = sum(
            1 for row in _queue_snapshot(db_a).values()
            if row["task_type"] == "review"
        )
        mcp_review_count = sum(
            1 for row in _queue_snapshot(db_b).values()
            if row["task_type"] == "review"
        )
        assert http_review_count == mcp_review_count == 1

    def test_failed_status_propagates_both_sides(self, parity_pair, tmp_path):
        http_client, _, db_a = parity_pair
        _enqueue_task(db_a, "p-sub-2")
        http_client.get("/tasks/next", params={"role": "implementer"})
        http_client.post(
            "/tasks/p-sub-2/result",
            json={
                "task_id": "p-sub-2",
                "status": "failed",
                "summary": "broken",
                "verdict": None,
                "findings": ["X failed"],
                "pr_number": None,
            },
        )

        db_b = str(tmp_path / "parity_mcp.db")
        _enqueue_task(db_b, "p-sub-2")
        mcp = build_mcp_server(db_b)
        _call_tool(mcp, "get_next_task", role="implementer")
        _call_tool(
            mcp,
            "submit_result",
            task_id="p-sub-2",
            status="failed",
            summary="broken",
            findings=["X failed"],
        )

        # Both should leave the task in failed (after auto-retry decisions
        # the server side may have spun out a retry task with a different
        # task_id — strip those to compare just the original task's state).
        snap_a = {
            tid: row for tid, row in _queue_snapshot(db_a).items()
            if tid == "p-sub-2"
        }
        snap_b = {
            tid: row for tid, row in _queue_snapshot(db_b).items()
            if tid == "p-sub-2"
        }
        assert snap_a == snap_b

    def test_unknown_task_id_errors_both_sides(self, parity_pair, tmp_path):
        http_client, _, _ = parity_pair
        http_resp = http_client.post(
            "/tasks/p-no-such/result",
            json={
                "task_id": "p-no-such",
                "status": "completed",
                "summary": "",
                "verdict": None,
                "findings": [],
                "pr_number": None,
            },
        )
        assert http_resp.status_code in (400, 404)

        db_b = str(tmp_path / "parity_mcp.db")
        TaskQueue(db_b)
        mcp = build_mcp_server(db_b)
        mcp_resp = _call_tool(
            mcp, "submit_result",
            task_id="p-no-such", status="completed",
        )
        assert mcp_resp["acknowledged"] is False
        assert "error" in mcp_resp


# ---------------------------------------------------------------------------
# GET /tasks/{id}  ↔  get_task
# ---------------------------------------------------------------------------


class TestGetTaskParity:
    def test_returns_same_payload_for_known_task(self, parity_pair, tmp_path):
        http_client, _, db_a = parity_pair
        _enqueue_task(db_a, "p-get-1", description="check this", priority=2)
        http_payload = http_client.get("/tasks/p-get-1").json()

        db_b = str(tmp_path / "parity_mcp.db")
        _enqueue_task(db_b, "p-get-1", description="check this", priority=2)
        mcp = build_mcp_server(db_b)
        mcp_payload = _call_tool(mcp, "get_task", task_id="p-get-1")

        assert _normalize_task_payload(http_payload) == _normalize_task_payload(
            mcp_payload
        )

    def test_unknown_task_diverges_intentionally(self, parity_pair, tmp_path):
        """HTTP raises 404; MCP returns None. Both signal "no such task" but
        through their native channels — record the contract here so a future
        unification doesn't quietly drift one way."""
        http_client, _, _ = parity_pair
        assert http_client.get("/tasks/p-missing").status_code == 404

        db_b = str(tmp_path / "parity_mcp.db")
        TaskQueue(db_b)
        mcp = build_mcp_server(db_b)
        assert _call_tool(mcp, "get_task", task_id="p-missing") is None


# ---------------------------------------------------------------------------
# GET /tasks?status=pending  ↔  list_pending
# ---------------------------------------------------------------------------


class TestListPendingParity:
    def test_pending_only_both_sides(self, parity_pair, tmp_path):
        http_client, _, db_a = parity_pair
        _enqueue_task(db_a, "p-list-1", task_type="implement")
        _enqueue_task(db_a, "p-list-2", task_type="review")
        http_payload = http_client.get(
            "/tasks", params={"status": "pending"}
        ).json()

        db_b = str(tmp_path / "parity_mcp.db")
        _enqueue_task(db_b, "p-list-1", task_type="implement")
        _enqueue_task(db_b, "p-list-2", task_type="review")
        mcp = build_mcp_server(db_b)
        mcp_payload = _call_tool(mcp, "list_pending")

        # Both must enumerate the same set — order-insensitive comparison.
        http_ids = sorted(t["task_id"] for t in http_payload)
        mcp_ids = sorted(t["task_id"] for t in mcp_payload)
        assert http_ids == mcp_ids

    def test_role_filter_on_mcp_matches_http_subset(self, parity_pair, tmp_path):
        """HTTP `GET /tasks` doesn't filter by role natively (only by status),
        but the agent-facing call ``list_pending(role=...)`` does. Asserting
        the MCP filter is consistent with what `GET /tasks?status=pending`
        followed by a client-side role match would yield."""
        http_client, _, db_a = parity_pair
        _enqueue_task(db_a, "p-list-3", task_type="implement")
        _enqueue_task(db_a, "p-list-4", task_type="review")
        http_payload = http_client.get(
            "/tasks", params={"status": "pending"}
        ).json()
        http_review_ids = sorted(
            t["task_id"] for t in http_payload if t["task_type"] == "review"
        )

        db_b = str(tmp_path / "parity_mcp.db")
        _enqueue_task(db_b, "p-list-3", task_type="implement")
        _enqueue_task(db_b, "p-list-4", task_type="review")
        mcp = build_mcp_server(db_b)
        mcp_payload = _call_tool(mcp, "list_pending", role="reviewer")
        mcp_ids = sorted(t["task_id"] for t in mcp_payload)
        assert mcp_ids == http_review_ids


# ---------------------------------------------------------------------------
# DELETE /tasks/{id}  ↔  cancel_task
# ---------------------------------------------------------------------------


class TestCancelTaskParity:
    def test_cancels_pending_task_both_sides(self, parity_pair, tmp_path):
        http_client, _, db_a = parity_pair
        _enqueue_task(db_a, "p-cancel-1")
        http_client.delete("/tasks/p-cancel-1")

        db_b = str(tmp_path / "parity_mcp.db")
        _enqueue_task(db_b, "p-cancel-1")
        mcp = build_mcp_server(db_b)
        mcp_resp = _call_tool(mcp, "cancel_task", task_id="p-cancel-1")
        assert mcp_resp["acknowledged"] is True

        assert _queue_snapshot(db_a) == _queue_snapshot(db_b)

    def test_cancel_unknown_id_idempotent_both_sides(self, parity_pair, tmp_path):
        http_client, _, _ = parity_pair
        # HTTP DELETE on unknown is silently OK (queue tolerates it).
        resp = http_client.delete("/tasks/p-no-such-cancel")
        assert resp.status_code in (200, 204)

        db_b = str(tmp_path / "parity_mcp.db")
        TaskQueue(db_b)
        mcp = build_mcp_server(db_b)
        mcp_resp = _call_tool(mcp, "cancel_task", task_id="p-no-such-cancel")
        assert mcp_resp["acknowledged"] is True
