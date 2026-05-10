"""Tests for bugs #152, #167, #159.

#152 — watchdog measures idle_for from push_at (not dequeue time)
#167 — force_fail records error_info; fallback loop detection
#159 — pending task dispatched immediately when result submitted
"""
import json

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


# ---------------------------------------------------------------------------
# helpers shared across tests
# ---------------------------------------------------------------------------

def _task(task_id="t1", task_type="implement", priority=3, ctx=None):
    return TaskRequest(
        task_id=task_id,
        task_type=task_type,
        description="do work",
        branch="main",
        priority=priority,
        context=ctx or {},
    )


def _post_task(client, task_id="t1", task_type="implement", priority=3, ctx=None):
    return client.post("/tasks", json={
        "task_id": task_id,
        "task_type": task_type,
        "description": "do work",
        "branch": "main",
        "priority": priority,
        "context": ctx or {},
    })


def _post_result(client, task_id, status="completed", summary="done"):
    return client.post(f"/tasks/{task_id}/result", json={
        "task_id": task_id,
        "status": status,
        "summary": summary,
        "verdict": None,
        "findings": [],
        "pr_number": None,
    })


class _RecordingPush:
    def __init__(self):
        self.calls: list = []

    def __call__(self, pane_id, text):
        self.calls.append((pane_id, text))


class _PaneState:
    def __init__(self):
        self.busy: dict = {}

    def set(self, pane_id, busy):
        self.busy[pane_id] = busy

    def __call__(self, pane_id):
        return self.busy.get(pane_id, False)


# ---------------------------------------------------------------------------
# #152 — push_at field
# ---------------------------------------------------------------------------

class TestPushAt:
    """Bug #152: idle clock must start from push_at, not from dequeue time."""

    # U-152-01: TaskQueue.set_push_at stores the timestamp.
    def test_u_152_01_set_push_at_stored(self, tmp_db):
        q = TaskQueue(tmp_db)
        q.enqueue(_task("t1"))
        q.dequeue(role="implementer")
        q.set_push_at("t1", ts=12345.0)
        rows = q.list_in_progress_with_activity()
        assert len(rows) == 1
        assert rows[0]["push_at"] == 12345.0

    # U-152-02: list_in_progress_with_activity includes push_at.
    def test_u_152_02_list_in_progress_includes_push_at(self, tmp_db):
        q = TaskQueue(tmp_db)
        q.enqueue(_task("t1"))
        q.dequeue(role="implementer")
        rows = q.list_in_progress_with_activity()
        assert "push_at" in rows[0], "push_at must be present in list_in_progress rows"

    # U-152-03: watchdog measures idle_for from push_at when it is set and <= now.
    def test_u_152_03_watchdog_idle_from_push_at(self, tmp_db):
        push = _RecordingPush()
        busy = _PaneState()
        app = create_app(
            db_path=tmp_db,
            pane_map={"implementer": "%100"},
            port=8100,
            push_fn=push,
            pane_busy_fn=busy,
            reminder_seconds=50.0,
            timeout_seconds=200.0,
            watchdog_disabled=True,
        )
        with TestClient(app) as client:
            _post_task(client, "t1")
            q = TaskQueue(tmp_db)
            # Override push_at so idle clock starts at 100, not real time.
            q.set_push_at("t1", ts=100.0)
            # Set last_activity_at earlier than push_at.
            q.bump_activity("t1", ts=50.0)
            # now=200: idle_for = 200 - max(push_at=100, last_activity_at=50) = 100s
            # 100 >= reminder_seconds(50) → reminder fires.
            result = app.state.watchdog_tick(now=200.0)

        assert "t1" in result["reminded"], (
            "idle_for should be 100s (from push_at=100) — reminder expected"
        )

    # U-152-04: task with push_at=0 (never pushed) falls back to last_activity_at.
    def test_u_152_04_unpushed_task_uses_last_activity(self, tmp_db):
        """If push_at is 0 (task dequeued but push_fn not called yet), watchdog
        falls back to last_activity_at for idle calculation."""
        push = _RecordingPush()
        busy = _PaneState()
        app = create_app(
            db_path=tmp_db,
            pane_map={"implementer": "%100"},
            port=8100,
            push_fn=push,
            pane_busy_fn=busy,
            reminder_seconds=50.0,
            timeout_seconds=200.0,
            watchdog_disabled=True,
        )
        with TestClient(app) as client:
            # Post the task (push fires → push_at set by _try_push_next)
            _post_task(client, "t2")
            q = TaskQueue(tmp_db)
            # Force push_at back to 0 to simulate an MCP-dequeued or not-yet-pushed task.
            q._reset_push_at("t2")
            # last_activity_at at 100 → idle_for = 200 - 100 = 100 ≥ reminder(50)
            q.bump_activity("t2", ts=100.0)
            result = app.state.watchdog_tick(now=200.0)

        assert "t2" in result["reminded"], (
            "When push_at=0, idle should fall back to last_activity_at"
        )

    # U-152-05: push_at is set in the DB when _try_push_next calls push_fn.
    def test_u_152_05_push_at_set_after_try_push_next(self, tmp_db):
        """When a task is dispatched via _try_push_next, push_at must be recorded."""
        push = _RecordingPush()
        busy = _PaneState()
        app = create_app(
            db_path=tmp_db,
            pane_map={"implementer": "%100"},
            port=8100,
            push_fn=push,
            pane_busy_fn=busy,
            watchdog_disabled=True,
        )
        with TestClient(app) as client:
            _post_task(client, "t3")

        rows = TaskQueue(tmp_db).list_in_progress_with_activity()
        assert rows, "task should be in_progress"
        assert rows[0]["push_at"] > 0, "push_at must be set after push_fn called"


# ---------------------------------------------------------------------------
# #167 — error recording + fallback loop detection
# ---------------------------------------------------------------------------

class TestFallbackLoop:
    """Bug #167: force_fail must record error_info; fallback chain must not loop forever."""

    # U-167-01: force_fail stores structured error_info in the DB.
    def test_u_167_01_force_fail_records_error_info(self, tmp_db):
        q = TaskQueue(tmp_db)
        q.enqueue(_task("t1"))
        q.dequeue(role="implementer")
        q.force_fail("t1", "watchdog timeout: pane idle 300s", error_info={"reason": "watchdog_timeout", "idle_seconds": 300})
        # Verify error_info is retrievable from the DB.
        conn = q._connect()
        row = conn.execute("SELECT error_info FROM tasks WHERE task_id = 't1'").fetchone()
        conn.close()
        assert row is not None
        stored = json.loads(row["error_info"])
        assert stored["reason"] == "watchdog_timeout"
        assert stored["idle_seconds"] == 300

    # U-167-02: fallback_chain_depth increments on each new fallback task.
    def test_u_167_02_fallback_chain_depth_tracked(self, tmp_db):
        """auto_fallback_failed_task increments fallback_chain_depth in the new task's context."""
        from agent_crew.pipeline import auto_fallback_failed_task

        q = TaskQueue(tmp_db)
        # Original task (no depth)
        q.enqueue(_task("orig", ctx={}))
        q.dequeue(role="implementer")
        result = TaskResult(
            task_id="orig",
            status="failed",
            summary="usage limit hit",
            verdict=None,
            findings=[],
        )
        auto_fallback_failed_task(
            q, "orig", result, "implement",
            pane_map={"implementer": "%1", "claude": "%1", "codex": "%2", "gemini": "%3"},
        )
        pending = q.list_tasks(status="pending")
        assert len(pending) == 1, "one fallback task should be created"
        fb_ctx = pending[0].context
        assert fb_ctx.get("fallback_chain_depth", 0) == 1, "depth should be 1 after first fallback"

    # U-167-03: fallback loop cancelled once chain depth reaches 3.
    def test_u_167_03_fallback_loop_cancelled_at_max_depth(self, tmp_db):
        """When fallback_chain_depth already == 3 in context, no new fallback is created."""
        from agent_crew.pipeline import auto_fallback_failed_task

        q = TaskQueue(tmp_db)
        # Simulate a task that's already at depth 3.
        q.enqueue(_task("fb3", ctx={
            "fallback_chain_depth": 3,
            "agent_override": "gemini",
            "fallback_excluded": ["claude", "codex", "gemini"],
        }))
        q.dequeue(role="implementer")
        result = TaskResult(
            task_id="fb3",
            status="failed",
            summary="usage limit hit",
            verdict=None,
            findings=[],
        )
        auto_fallback_failed_task(
            q, "fb3", result, "implement",
            pane_map={"implementer": "%1", "claude": "%1", "codex": "%2", "gemini": "%3"},
        )
        pending = q.list_tasks(status="pending")
        assert len(pending) == 0, (
            "No new fallback task should be created when depth >= 3 (loop detection)"
        )

    # U-167-04: force_fail with no error_info stores NULL (backwards compatible).
    def test_u_167_04_force_fail_without_error_info_is_null(self, tmp_db):
        q = TaskQueue(tmp_db)
        q.enqueue(_task("t1"))
        q.dequeue(role="implementer")
        q.force_fail("t1", "watchdog timeout")
        conn = q._connect()
        row = conn.execute("SELECT error_info FROM tasks WHERE task_id = 't1'").fetchone()
        conn.close()
        assert row["error_info"] is None


# ---------------------------------------------------------------------------
# #159 — pending task dispatched immediately on result submission
# ---------------------------------------------------------------------------

class TestDispatchOnResult:
    """Bug #159: when a result is submitted, any pending task for the same role
    must be dispatched to the pane immediately (no 120s stale-pending wait)."""

    # U-159-01: pending task B dispatched when task A result arrives.
    def test_u_159_01_pending_dispatched_immediately_on_result(self, tmp_db):
        push = _RecordingPush()
        app = create_app(
            db_path=tmp_db,
            pane_map={"implementer": "%100"},
            port=8100,
            push_fn=push,
            watchdog_disabled=True,
        )
        with TestClient(app) as client:
            _post_task(client, "A")
            _post_task(client, "B")          # stays pending (A in progress)
            assert len(push.calls) == 1      # only A pushed

            _post_result(client, "A")        # A done → B dispatched immediately

        assert len(push.calls) == 2, "B must be dispatched when A completes"
        assert "B" in push.calls[1][1]

    # U-159-02: after failed result, retry/fallback task dispatched for same pane.
    def test_u_159_02_dispatch_after_failed_with_pending(self, tmp_db):
        push = _RecordingPush()
        app = create_app(
            db_path=tmp_db,
            pane_map={"implementer": "%100"},
            port=8100,
            push_fn=push,
            watchdog_disabled=True,
            fallback_disabled=True,   # disable fallback so retry path is taken
        )
        with TestClient(app) as client:
            _post_task(client, "A")
            _post_task(client, "B")
            assert len(push.calls) == 1

            # A fails (non-rate-limit) → auto-retry creates retry-A → B still pending.
            # _try_push_next fires → retry-A gets dispatched (has lower priority).
            # OR B gets dispatched first if B has higher priority.
            # Either way, a push fires.
            _post_result(client, "A", status="failed", summary="generic failure")

        # Either B or retry-A pushed — at minimum one additional push.
        assert len(push.calls) >= 2, "A push must fire after failed result + pending tasks"


# ---------------------------------------------------------------------------
# Review feedback: wire error_info into real failure paths; cancel chain on loop
# ---------------------------------------------------------------------------

class TestErrorInfoWiring:
    """Review feedback for #167: error_info must be stored in ALL failure paths,
    not just the force_fail signature."""

    # U-167-05: watchdog force_fail wires structured error_info into DB.
    def test_u_167_05_watchdog_force_fail_includes_error_info(self, tmp_db):
        push = _RecordingPush()
        busy = _PaneState()
        app = create_app(
            db_path=tmp_db,
            pane_map={"implementer": "%100"},
            port=8100,
            push_fn=push,
            pane_busy_fn=busy,
            reminder_seconds=50.0,
            timeout_seconds=100.0,
            watchdog_disabled=True,
        )
        with TestClient(app) as client:
            _post_task(client, "t1")
            q = TaskQueue(tmp_db)
            q.set_push_at("t1", ts=50.0)
            q.bump_activity("t1", ts=50.0)
            # Add to reminded set so the timeout path fires (not just reminder).
            app.state.reminded_task_ids.add("t1")
            # now=300: idle_for=250 >= timeout(100) and reminded → force_fail.
            app.state.watchdog_tick(now=300.0)

        conn = TaskQueue(tmp_db)._connect()
        row = conn.execute(
            "SELECT error_info, status FROM tasks WHERE task_id = 't1'"
        ).fetchone()
        conn.close()
        assert row["status"] == "failed"
        assert row["error_info"] is not None, (
            "watchdog force_fail must store structured error_info in DB"
        )
        info = json.loads(row["error_info"])
        assert info.get("reason") == "watchdog_timeout"
        assert "idle_seconds" in info

    # U-167-06: submit_result with status='failed' stores error_info from TaskResult.
    def test_u_167_06_submit_result_failed_stores_error_info(self, tmp_db):
        q = TaskQueue(tmp_db)
        q.enqueue(_task("t1"))
        q.dequeue(role="implementer")
        result = TaskResult(
            task_id="t1",
            status="failed",
            summary="api stream cut",
            verdict=None,
            findings=[],
            error_info={"reason": "api_stream_timeout", "details": "no response after 120s"},
        )
        q.submit_result("t1", result)
        conn = q._connect()
        row = conn.execute("SELECT error_info FROM tasks WHERE task_id = 't1'").fetchone()
        conn.close()
        assert row["error_info"] is not None
        stored = json.loads(row["error_info"])
        assert stored["reason"] == "api_stream_timeout"


class TestFallbackCancellation:
    """Review feedback: fallback loop guard must cancel the original task,
    not just open a gate; auto_retry must use DB retry count."""

    # U-167-07: fallback chain loop cancels the original_task_id task.
    def test_u_167_07_fallback_chain_cancels_original_on_loop(self, tmp_db):
        """When fallback_chain_depth >= MAX, the original_task_id task must be
        marked 'cancelled' — not just have an escalation gate opened."""
        from agent_crew.pipeline import auto_fallback_failed_task

        q = TaskQueue(tmp_db)
        # Seed the root original task as already failed.
        q.enqueue(_task("orig-root"))
        q.dequeue(role="implementer")
        q.force_fail("orig-root", "first failure")

        # Simulate a fallback at depth=3 that traces back to orig-root.
        q.enqueue(_task("fb3", ctx={
            "fallback_chain_depth": 3,
            "agent_override": "gemini",
            "fallback_excluded": ["claude", "codex", "gemini"],
            "original_task_id": "orig-root",
        }))
        q.dequeue(role="implementer")
        result = TaskResult(
            task_id="fb3",
            status="failed",
            summary="usage limit hit",
            verdict=None,
            findings=[],
        )
        auto_fallback_failed_task(
            q, "fb3", result, "implement",
            pane_map={"implementer": "%1", "claude": "%1", "codex": "%2", "gemini": "%3"},
        )

        pending = q.list_tasks(status="pending")
        assert len(pending) == 0, "No new pending task on loop detection"

        orig_tasks = [t for t in q.list_tasks() if t.task_id == "orig-root"]
        assert orig_tasks[0].status == "cancelled", (
            "original_task_id must be cancelled when fallback loop is detected"
        )

    # U-167-08: auto_retry reads retry count from DB context, not result.retry_count.
    def test_u_167_08_auto_retry_uses_db_retry_count(self, tmp_db):
        """result.retry_count is always 0 from agents; the server must check
        context.retry_attempt stored in the DB to prevent infinite retries."""
        push = _RecordingPush()
        app = create_app(
            db_path=tmp_db,
            pane_map={"implementer": "%100"},
            port=8100,
            push_fn=push,
            watchdog_disabled=True,
            fallback_disabled=True,
        )
        with TestClient(app) as client:
            # Enqueue a task that has already been retried twice (at max).
            _post_task(client, "A", ctx={"retry_attempt": 2})
            # Agent submits failure with retry_count=0 (agents never fill this).
            _post_result(client, "A", status="failed", summary="generic failure")

        q = TaskQueue(tmp_db)
        # The retry task must NOT have been created — check pending + in_progress.
        # (In tests, _pane_alive_for_push=True so a retry task would be immediately
        #  pushed to in_progress, not left pending — check all active statuses.)
        active = [t for t in q.list_tasks() if t.status in ("pending", "in_progress")]
        assert len(active) == 0, (
            "No retry task (in any active status) when DB context.retry_attempt >= MAX_RETRIES"
        )
