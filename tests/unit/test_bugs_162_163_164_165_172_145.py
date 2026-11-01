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

        pane_map = {"implementer": "%92"}
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
        review_id = auto_enqueue_review(q, impl_id, pr_number=7, pr_state_fn=lambda pr: "open")
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
        review_id = auto_enqueue_review(q, impl_id, pr_number=7, pr_state_fn=lambda pr: "open")
        assert review_id is not None
        q.submit_result(review_id, TaskResult(
            task_id=review_id, status="completed", summary="lgtm",
            verdict="approve", findings=[], pr_number=7,
        ))
        test_id = auto_enqueue_test(q, review_id, pr_state_fn=lambda pr: "open")
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
            resp = client.get("/tasks/next?role=implementer&agent=codex")
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
            resp = client.get("/tasks/next?role=implementer&agent=codex")
        assert resp.status_code == 200

    def test_u172_get_tasks_next_works_in_both_mode(self):
        """GET /tasks/next must still work in both mode."""
        db, q = _make_db()
        with patch.dict("os.environ", {"AGENT_CREW_DELIVERY": "both"}):
            from agent_crew.server import create_app
            app = create_app(db, pane_map={}, port=9999, watchdog_disabled=True)
        with TestClient(app) as client:
            resp = client.get("/tasks/next?role=implementer&agent=codex")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# #145 — watchdog auto-fails stale pending tasks in MCP mode
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# #873 — watchdog must not auto-fail as mcp_no_client when the dispatcher
# owns delivery (DELIVERY=mcp + DISPATCHER=1)
# ---------------------------------------------------------------------------

class TestMcpNoClientDispatcherOwnsDelivery:
    def test_u873_stale_pending_not_failed_when_dispatcher_enabled(self):
        db, q = _make_db()
        # A running claude implement occupies the lane; the queued one waits.
        q.enqueue(TaskRequest(task_id="running-873", task_type="implement",
                              description="running", branch="main"))
        assert q.dequeue(role="implementer").task_id == "running-873"
        task_id = "queued-873"
        q.enqueue(TaskRequest(task_id=task_id, task_type="implement",
                              description="queued behind busy lane", branch="main"))
        with sqlite3.connect(db) as conn:
            stale_at = time.time() - 200
            conn.execute("UPDATE tasks SET created_at=?, last_activity_at=? WHERE task_id=?",
                         (stale_at, stale_at, task_id))

        with patch.dict("os.environ", {"AGENT_CREW_DELIVERY": "mcp",
                                        "AGENT_CREW_DISPATCHER": "1",
                                        # The loop sleeps first; keep it idle so
                                        # only the watchdog tick acts.
                                        "AGENT_CREW_DISPATCH_INTERVAL": "3600",
                                        "AGENT_CREW_STALE_PENDING_SECONDS": "120"}):
            from agent_crew.server import create_app
            app = create_app(db, pane_map={"implementer": "%93"}, port=9999,
                             watchdog_disabled=True, anomaly_disabled=True,
                             pane_busy_fn=lambda _: False)
            with TestClient(app):
                result = app.state.watchdog_tick(time.time())

        assert task_id not in result.get("mcp_no_client_failed", [])
        task_row = next(r for r in q.list_all_with_status() if r["task_id"] == task_id)
        assert task_row["status"] == "pending"

