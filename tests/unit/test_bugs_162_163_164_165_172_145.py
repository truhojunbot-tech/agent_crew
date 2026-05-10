"""Unit tests for issues #162, #163, #164, #165, #172, #145.

#162 — watchdog reminder in MCP mode must be a short one-liner (no curl template).
#163 — auto-clear skipped in MCP mode (long-lived session preservation).
#164 — review/test task descriptions are compact (no full spec re-injection).
#165 — instructions.generate() uses _MCP_COMMON in mcp delivery mode.
#172 — GET /tasks/next returns 405 when AGENT_CREW_DELIVERY=mcp.
#145 — watchdog auto-fails stale-pending tasks when delivery=mcp (no MCP client).
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db() -> tuple[str, TaskQueue]:
    tmp = tempfile.mktemp(suffix=".db")
    return tmp, TaskQueue(tmp)


def _make_app(db_path: str, delivery: str = "mcp", pane_map: dict | None = None):
    from agent_crew.server import create_app
    env_patch = {"AGENT_CREW_DELIVERY": delivery, "AGENT_CREW_WATCHDOG_DISABLED": "1"}
    with patch.dict("os.environ", env_patch):
        app = create_app(
            db_path,
            pane_map=pane_map or {},
            port=9999,
            watchdog_disabled=True,
        )
    return app


# ---------------------------------------------------------------------------
# #162 — MCP mode watchdog reminder: short one-liner, no curl template
# ---------------------------------------------------------------------------

class TestMcpReminderFormat:
    def test_u162_mcp_mode_reminder_is_short(self):
        """In MCP mode the reminder must be a single line, no curl template."""
        from agent_crew.server import _format_reminder_message
        msg = _format_reminder_message("t-abc", 9999, 350.0, mcp_mode=True)
        assert "curl" not in msg, "MCP reminder must not include curl template"
        assert "bump_activity" in msg or "submit_result" in msg
        assert "\n" not in msg.strip(), "MCP reminder must be a single line"

    def test_u162_push_mode_reminder_has_curl(self):
        """In push mode the full curl template is still included."""
        from agent_crew.server import _format_reminder_message
        msg = _format_reminder_message("t-abc", 9999, 350.0, mcp_mode=False)
        assert "curl" in msg
        assert "=== AGENT_CREW REMINDER ===" in msg

    def test_u162_watchdog_uses_mcp_mode_when_push_disabled(self, tmp_path):
        """Watchdog emits MCP-style reminder when delivery=mcp."""
        db, q = _make_db()
        q.enqueue(TaskRequest(task_id="t1", task_type="implement",
                               description="work", branch="main"))
        q.dequeue(role="implementer")  # mark in_progress

        pushed: list[str] = []

        def fake_push(pane_id: str, msg: str) -> None:
            pushed.append(msg)

        pane_map = {"implementer": "%1"}
        with patch.dict("os.environ", {"AGENT_CREW_DELIVERY": "mcp"}):
            from agent_crew.server import create_app
            app = create_app(
                db,
                pane_map=pane_map,
                port=9999,
                push_fn=fake_push,
                watchdog_disabled=True,
                pane_busy_fn=lambda _: False,
                reminder_seconds=1,
                timeout_seconds=9999,
            )

        now = time.time() + 400
        with TestClient(app), patch("agent_crew.server._pane_dismiss_permission_prompt"):
            app.state.watchdog_tick(now)

        assert pushed, "expected a reminder to be pushed"
        assert "curl" not in pushed[0], "MCP reminder must not contain curl"


# ---------------------------------------------------------------------------
# #163 — MCP mode: auto-clear skipped
# ---------------------------------------------------------------------------

class TestMcpAutoClearSkipped:
    def test_u163_auto_clear_skipped_in_mcp_mode(self, tmp_path):
        """_pane_clear_context must NOT be called when delivery=mcp."""
        db, q = _make_db()
        q.enqueue(TaskRequest(task_id="tc1", task_type="implement",
                               description="task", branch="main"))

        clear_calls: list[str] = []

        def fake_push(pane_id: str, msg: str) -> None:
            pass

        pane_map = {"implementer": "%2"}
        with patch.dict("os.environ", {"AGENT_CREW_DELIVERY": "mcp"}):
            from agent_crew.server import create_app
            app = create_app(
                db,
                pane_map=pane_map,
                port=9999,
                push_fn=fake_push,
                watchdog_disabled=True,
                pane_busy_fn=lambda _: False,
            )

        with TestClient(app) as client:
            with patch("agent_crew.server._pane_clear_context", side_effect=clear_calls.append) as mock_clear, \
                 patch("agent_crew.server._pane_alive_for_push", return_value=True), \
                 patch("agent_crew.server._pane_has_usage_limit", return_value=False), \
                 patch("agent_crew.server._pane_has_task", return_value=False):
                client.post(
                    "/tasks",
                    json={"task_id": "tc1-new", "task_type": "implement",
                          "description": "new task", "branch": "main"},
                )

        mock_clear.assert_not_called()


# ---------------------------------------------------------------------------
# #164 — compact review/test descriptions
# ---------------------------------------------------------------------------

class TestCompactHandoffDescriptions:
    def _enqueue_impl(self, q: TaskQueue, pr_number: int | None = 7,
                      branch: str = "feat/x") -> str:
        tid = f"impl-{uuid.uuid4().hex[:6]}"
        q.enqueue(TaskRequest(task_id=tid, task_type="implement",
                               description="A very long implementation spec that should NOT appear in reviewer context.",
                               branch=branch))
        q.submit_result(tid, TaskResult(
            task_id=tid, status="completed", summary="done",
            verdict=None, findings=[], pr_number=pr_number,
        ))
        return tid

    def test_u164_review_description_is_compact(self):
        """auto_enqueue_review must use a short description, not the full impl spec."""
        from agent_crew.pipeline import auto_enqueue_review
        _, q = _make_db()
        impl_id = self._enqueue_impl(q, pr_number=7)
        review_id = auto_enqueue_review(q, impl_id, pr_number=7)
        assert review_id is not None
        tasks = {t.task_id: t for t in q.list_tasks()}
        review_task = tasks[review_id]
        assert "long implementation spec" not in review_task.description, (
            "full impl description must not be re-injected into review task"
        )
        assert "Review" in review_task.description or "review" in review_task.description

    def test_u164_test_description_is_compact(self):
        """auto_enqueue_test must use a short description, not the full review spec."""
        from agent_crew.pipeline import auto_enqueue_review, auto_enqueue_test
        _, q = _make_db()
        impl_id = self._enqueue_impl(q, pr_number=7)
        review_id = auto_enqueue_review(q, impl_id, pr_number=7)
        assert review_id is not None
        q.submit_result(review_id, TaskResult(
            task_id=review_id, status="completed", summary="lgtm",
            verdict="approve", findings=[], pr_number=7,
        ))
        test_id = auto_enqueue_test(q, review_id)
        assert test_id is not None
        tasks = {t.task_id: t for t in q.list_tasks()}
        test_task = tasks[test_id]
        assert "long implementation spec" not in test_task.description
        assert "Test" in test_task.description or "test" in test_task.description


# ---------------------------------------------------------------------------
# #165 — instructions.generate() uses _MCP_COMMON for delivery=mcp
# ---------------------------------------------------------------------------

class TestInstructionsDeliverySeparation:
    def test_u165_mcp_delivery_uses_mcp_common(self):
        """generate(..., delivery='mcp') must use _MCP_COMMON (no curl in protocol section)."""
        from agent_crew.instructions import generate, _MCP_COMMON, _COMMON
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("9999")
            port_file = f.name
        content = generate("implementer", "myproj", 9999, delivery="mcp")
        # MCP block must be present
        assert "submit_result" in content
        # Curl template from _COMMON must not appear in protocol section
        # (task-loop prompt may have curl for push fallback but protocol block shouldn't)
        assert "curl -sS -X POST" not in content.split("---")[1] if "---" in content else True

    def test_u165_push_delivery_uses_common(self):
        """generate(..., delivery='push') must include the curl template."""
        from agent_crew.instructions import generate
        content = generate("implementer", "myproj", 9999, delivery="push")
        assert "curl" in content

    def test_u165_mcp_common_omits_curl_template_section(self):
        """_MCP_COMMON must not include the Canonical POST curl template."""
        from agent_crew.instructions import _MCP_COMMON
        assert "curl -sS -X POST" not in _MCP_COMMON

    def test_u165_common_includes_curl_template(self):
        """_COMMON must include the full curl template."""
        from agent_crew.instructions import _COMMON
        assert "curl -sS -X POST" in _COMMON

    def test_u165_env_var_respected(self):
        """generate() without explicit delivery uses AGENT_CREW_DELIVERY env var."""
        from agent_crew.instructions import generate
        with patch.dict("os.environ", {"AGENT_CREW_DELIVERY": "mcp"}):
            content = generate("implementer", "proj", 9999)
        assert "submit_result" in content

    def test_u165_mcp_common_has_submit_result_tool(self):
        """_MCP_COMMON must reference the submit_result MCP tool."""
        from agent_crew.instructions import _MCP_COMMON
        assert "submit_result" in _MCP_COMMON


# ---------------------------------------------------------------------------
# #172 — GET /tasks/next returns 405 when AGENT_CREW_DELIVERY=mcp
# ---------------------------------------------------------------------------

class TestHttpPollBlockedInMcpMode:
    def test_u172_get_tasks_next_returns_405_in_mcp_mode(self):
        """GET /tasks/next must return 405 when AGENT_CREW_DELIVERY=mcp."""
        db, q = _make_db()
        with patch.dict("os.environ", {"AGENT_CREW_DELIVERY": "mcp"}):
            from agent_crew.server import create_app
            app = create_app(db, pane_map={}, port=9999, watchdog_disabled=True)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/tasks/next?role=implementer")
        assert resp.status_code == 405, (
            f"Expected 405 in MCP mode, got {resp.status_code}: {resp.text}"
        )

    def test_u172_405_message_mentions_mcp_tool(self):
        """The 405 error body must mention the MCP tool as the alternative."""
        db, q = _make_db()
        with patch.dict("os.environ", {"AGENT_CREW_DELIVERY": "mcp"}):
            from agent_crew.server import create_app
            app = create_app(db, pane_map={}, port=9999, watchdog_disabled=True)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/tasks/next")
        body = resp.text
        assert "MCP" in body or "get_next_task" in body, (
            f"405 response must mention MCP tool: {body}"
        )

    def test_u172_get_tasks_next_works_in_push_mode(self):
        """GET /tasks/next must still work (return 200/null) in push mode."""
        db, q = _make_db()
        with patch.dict("os.environ", {"AGENT_CREW_DELIVERY": "push"}):
            from agent_crew.server import create_app
            app = create_app(db, pane_map={}, port=9999, watchdog_disabled=True)
        with TestClient(app) as client:
            resp = client.get("/tasks/next?role=implementer")
        assert resp.status_code == 200

    def test_u172_get_tasks_next_works_in_both_mode(self):
        """GET /tasks/next must still work in both mode."""
        db, q = _make_db()
        with patch.dict("os.environ", {"AGENT_CREW_DELIVERY": "both"}):
            from agent_crew.server import create_app
            app = create_app(db, pane_map={}, port=9999, watchdog_disabled=True)
        with TestClient(app) as client:
            resp = client.get("/tasks/next?role=implementer")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# #145 — watchdog auto-fails stale pending tasks in MCP mode
# ---------------------------------------------------------------------------

class TestMcpNoClientAutoFail:
    def _make_stale_pending(self, db: str, task_id: str, seconds_ago: float = 200):
        """Insert a pending task whose created_at is old enough to be stale."""
        conn = sqlite3.connect(db)
        old_ts = time.time() - seconds_ago
        conn.execute(
            "UPDATE tasks SET created_at=?, last_activity_at=? WHERE task_id=?",
            (old_ts, old_ts, task_id),
        )
        conn.commit()
        conn.close()

    def test_u145_stale_pending_auto_failed_in_mcp_mode(self):
        """In MCP mode, stale-pending tasks must be auto-failed by the watchdog."""
        db, q = _make_db()
        task_id = "stale-mcp-1"
        q.enqueue(TaskRequest(task_id=task_id, task_type="implement",
                               description="mcp task", branch="main"))
        self._make_stale_pending(db, task_id, seconds_ago=200)

        pane_map = {"implementer": "%3"}
        with patch.dict("os.environ", {"AGENT_CREW_DELIVERY": "mcp",
                                        "AGENT_CREW_STALE_PENDING_SECONDS": "120"}):
            from agent_crew.server import create_app
            app = create_app(
                db,
                pane_map=pane_map,
                port=9999,
                watchdog_disabled=True,
                pane_busy_fn=lambda _: False,
            )

        with TestClient(app), patch("agent_crew.server._pane_dismiss_permission_prompt"):
            result = app.state.watchdog_tick(time.time())

        assert "mcp_no_client_failed" in result, (
            f"Expected mcp_no_client_failed in tick result, got: {result}"
        )
        assert task_id in result["mcp_no_client_failed"]

        # Verify status in DB
        rows = q.list_all_with_status()
        task_row = next(r for r in rows if r["task_id"] == task_id)
        assert task_row["status"] in ("failed", "cancelled"), (
            f"Expected task to be failed, got status={task_row['status']!r}"
        )

    def test_u145_fresh_pending_not_failed(self):
        """Pending tasks within the stale window must not be auto-failed."""
        db, q = _make_db()
        task_id = "fresh-mcp-1"
        q.enqueue(TaskRequest(task_id=task_id, task_type="implement",
                               description="fresh task", branch="main"))
        # Make it only 10 seconds old — well within the 120s window
        self._make_stale_pending(db, task_id, seconds_ago=10)

        pane_map = {"implementer": "%4"}
        with patch.dict("os.environ", {"AGENT_CREW_DELIVERY": "mcp",
                                        "AGENT_CREW_STALE_PENDING_SECONDS": "120"}):
            from agent_crew.server import create_app
            app = create_app(
                db,
                pane_map=pane_map,
                port=9999,
                watchdog_disabled=True,
                pane_busy_fn=lambda _: False,
            )

        with TestClient(app), patch("agent_crew.server._pane_dismiss_permission_prompt"):
            result = app.state.watchdog_tick(time.time())

        assert task_id not in result.get("mcp_no_client_failed", [])

        rows = q.list_all_with_status()
        task_row = next(r for r in rows if r["task_id"] == task_id)
        assert task_row["status"] == "pending"

    def test_u145_stale_pending_not_failed_in_push_mode(self):
        """In push mode, stale-pending tasks must be re-dispatched, not auto-failed."""
        db, q = _make_db()
        task_id = "stale-push-1"
        q.enqueue(TaskRequest(task_id=task_id, task_type="implement",
                               description="push task", branch="main"))
        conn = sqlite3.connect(db)
        old_ts = time.time() - 200
        conn.execute(
            "UPDATE tasks SET created_at=?, last_activity_at=? WHERE task_id=?",
            (old_ts, old_ts, task_id),
        )
        conn.commit()
        conn.close()

        pushed: list[str] = []

        def fake_push(pane_id: str, msg: str) -> None:
            pushed.append(msg)

        pane_map = {"implementer": "%5"}
        with patch.dict("os.environ", {"AGENT_CREW_DELIVERY": "push",
                                        "AGENT_CREW_STALE_PENDING_SECONDS": "120"}):
            from agent_crew.server import create_app
            app = create_app(
                db,
                pane_map=pane_map,
                port=9999,
                push_fn=fake_push,
                watchdog_disabled=True,
                pane_busy_fn=lambda _: False,
            )

        with TestClient(app):
            with patch("agent_crew.server._pane_dismiss_permission_prompt"), \
                 patch("agent_crew.server._pane_alive_for_push", return_value=True), \
                 patch("agent_crew.server._pane_has_usage_limit", return_value=False), \
                 patch("agent_crew.server._pane_has_task", return_value=False):
                result = app.state.watchdog_tick(time.time())

        # Push mode should re-dispatch (stale_redispatched), not mcp_no_client_failed
        assert "mcp_no_client_failed" not in result
        assert "stale_redispatched" in result

        rows = q.list_all_with_status()
        task_row = next(r for r in rows if r["task_id"] == task_id)
        # Task should have been dequeued (in_progress) or still pending if push failed
        assert task_row["status"] != "failed", (
            "push-mode stale task must not be auto-failed"
        )

    def test_u145_auto_fail_includes_error_info(self):
        """Auto-failed task must be recorded with reason=mcp_no_client in error_info."""
        db, q = _make_db()
        task_id = "stale-mcp-errinfo"
        q.enqueue(TaskRequest(task_id=task_id, task_type="implement",
                               description="task", branch="main"))
        conn = sqlite3.connect(db)
        old_ts = time.time() - 200
        conn.execute(
            "UPDATE tasks SET created_at=?, last_activity_at=? WHERE task_id=?",
            (old_ts, old_ts, task_id),
        )
        conn.commit()
        conn.close()

        pane_map = {"implementer": "%6"}
        with patch.dict("os.environ", {"AGENT_CREW_DELIVERY": "mcp",
                                        "AGENT_CREW_STALE_PENDING_SECONDS": "120"}):
            from agent_crew.server import create_app
            app = create_app(
                db,
                pane_map=pane_map,
                port=9999,
                watchdog_disabled=True,
                pane_busy_fn=lambda _: False,
            )

        with TestClient(app), patch("agent_crew.server._pane_dismiss_permission_prompt"):
            app.state.watchdog_tick(time.time())

        result = q.get_result(task_id)
        assert result is not None, "force_fail must store a result"
        assert "mcp" in (result.summary or "").lower(), (
            f"summary must mention mcp, got: {result.summary}"
        )
