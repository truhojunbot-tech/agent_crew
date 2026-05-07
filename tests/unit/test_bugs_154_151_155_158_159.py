"""Tests for issues #154, #151, #155, #158, #159.

#154 — GET /tasks missing status field
#151 — Fallback routes to rate-limited codex pane
#155 — crew recover --reset-stale flag
#158 — Gemini pane crash: task push falls into bash
#159 — crew run hangs forever when agents busy (pending task timeout)
"""
import json
import sqlite3
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _impl_payload(task_id="t1", description="do work"):
    return {
        "task_id": task_id,
        "task_type": "implement",
        "description": description,
        "branch": "main",
        "priority": 3,
        "context": {},
        "project": "",
    }


def _make_app(tmp_db, *, push_calls=None, panes=None, **kwargs):
    panes = panes or {
        "implementer": "%C", "claude": "%C",
        "reviewer": "%X", "codex": "%X",
        "tester": "%G", "gemini": "%G",
    }
    if push_calls is None:
        push_calls = []

    def push(pane_id, text):
        push_calls.append((pane_id, text))

    return create_app(
        db_path=tmp_db,
        pane_map=panes,
        port=8200,
        push_fn=push,
        watchdog_disabled=True,
        **kwargs,
    ), push_calls


# ---------------------------------------------------------------------------
# #154 — GET /tasks response must include status field
# ---------------------------------------------------------------------------

class TestGetTasksStatusField:
    def test_get_tasks_list_includes_status(self, tmp_db):
        """GET /tasks returns a list of tasks each with a 'status' field.
        We create a app with no pane_map so tasks stay pending after enqueue."""
        app = create_app(db_path=tmp_db, pane_map=None, watchdog_disabled=True)
        with TestClient(app) as client:
            client.post("/tasks", json=_impl_payload("t1"))
            resp = client.get("/tasks")
        assert resp.status_code == 200
        tasks = resp.json()
        assert len(tasks) == 1
        assert "status" in tasks[0], "status field missing from GET /tasks response"
        assert tasks[0]["status"] == "pending"

    def test_get_tasks_filtered_by_status_returns_status_field(self, tmp_db):
        """GET /tasks?status=pending also includes status in each task."""
        app = create_app(db_path=tmp_db, pane_map=None, watchdog_disabled=True)
        with TestClient(app) as client:
            client.post("/tasks", json=_impl_payload("t2"))
            resp = client.get("/tasks?status=pending")
        assert resp.status_code == 200
        tasks = resp.json()
        assert len(tasks) == 1
        assert tasks[0]["status"] == "pending"

    def test_get_task_by_id_includes_status(self, tmp_db):
        """GET /tasks/{task_id} includes a 'status' field."""
        app = create_app(db_path=tmp_db, pane_map=None, watchdog_disabled=True)
        with TestClient(app) as client:
            client.post("/tasks", json=_impl_payload("t3"))
            resp = client.get("/tasks/t3")
        assert resp.status_code == 200
        task = resp.json()
        assert "status" in task, "status field missing from GET /tasks/{id} response"
        assert task["status"] == "pending"

    def test_status_reflects_in_progress_after_dequeue(self, tmp_db):
        """After the task is dequeued (in_progress), GET /tasks shows updated status."""
        app = create_app(db_path=tmp_db, pane_map=None, watchdog_disabled=True)
        with TestClient(app) as client:
            client.post("/tasks", json=_impl_payload("t4"))
            # Dequeue via GET /tasks/next (transitions pending → in_progress)
            client.get("/tasks/next?role=implementer")
            resp = client.get("/tasks/t4")
        task = resp.json()
        assert task["status"] == "in_progress"


# ---------------------------------------------------------------------------
# #151 — Fallback routes to rate-limited codex pane
# The fix: when a fallback task targets an agent whose pane is usage-limited,
# _try_push_next must detect it and trigger another fallback (to the next
# agent in the chain) instead of silently failing to push.
# ---------------------------------------------------------------------------

class TestUsageLimitOnFallbackAgentPaneReroutes:
    def test_usage_limit_on_fallback_agent_triggers_chain_again(self, tmp_db):
        """
        Scenario: original task fails (claude rate-limited) → fallback creates
        task with agent_override='codex'. When that task is dequeued for push,
        codex pane also shows a usage-limit → server must force-fail the fallback
        task and create a new one targeting gemini.
        """
        app, push_calls = _make_app(tmp_db)

        # Mark codex pane (%X) as usage-limited
        def pane_has_usage_limit(pane_id: str) -> bool:
            return pane_id == "%X"

        with TestClient(app) as client, \
             patch("agent_crew.server._pane_has_usage_limit", pane_has_usage_limit):
            # Enqueue the fallback task (claude already excluded)
            fallback_payload = _impl_payload("fb-t1")
            fallback_payload["context"] = {
                "agent_override": "codex",
                "fallback_excluded": ["claude"],
            }
            client.post("/tasks", json=fallback_payload)

        # The fallback task targeting codex should have been force-failed
        rows = TaskQueue(tmp_db).list_all_with_status()
        fb_task = next((r for r in rows if r["task_id"] == "fb-t1"), None)
        assert fb_task is not None
        assert fb_task["status"] == "failed", (
            "fallback task should have been force-failed when codex pane is usage-limited"
        )

        # A new fallback task for gemini should have been created
        gemini_tasks = [
            r for r in rows
            if isinstance(r.get("context"), dict)
            and r["context"].get("agent_override") == "gemini"
        ]
        assert len(gemini_tasks) >= 1, (
            "expected a new fallback task with agent_override='gemini' after codex usage-limit"
        )
        assert "codex" in gemini_tasks[0]["context"]["fallback_excluded"]

    def test_codex_push_proceeds_when_pane_healthy(self, tmp_db):
        """Regression: when codex pane is healthy, fallback task is pushed normally."""
        app, push_calls = _make_app(tmp_db)

        with TestClient(app) as client, \
             patch("agent_crew.server._pane_has_usage_limit", return_value=False):
            fallback_payload = _impl_payload("fb-t2")
            fallback_payload["context"] = {
                "agent_override": "codex",
                "fallback_excluded": ["claude"],
            }
            client.post("/tasks", json=fallback_payload)

        # Task should have been pushed to codex pane (%X)
        assert any(pane == "%X" for pane, _ in push_calls), (
            "healthy codex pane should receive the fallback task push"
        )


# ---------------------------------------------------------------------------
# #155 — crew recover --reset-stale
# ---------------------------------------------------------------------------

class TestRecoverResetStale:
    """
    crew recover --reset-stale cancels in_progress tasks idle longer than
    --stale-seconds (default 600s). The queue.expire_stale() method is the
    unit under test; the CLI wires it up in the recover command.
    """

    def test_expire_stale_cancels_old_in_progress_tasks(self, tmp_db):
        """expire_stale cancels tasks idle > older_than_seconds."""
        q = TaskQueue(tmp_db)
        q.enqueue(TaskRequest(
            task_id="s1", task_type="implement",
            description="stale task", branch="main",
        ))
        # Manually put it in_progress with an old timestamp
        conn = sqlite3.connect(tmp_db)
        old_ts = time.time() - 1200  # 20 minutes ago
        conn.execute(
            "UPDATE tasks SET status='in_progress', last_activity_at=? WHERE task_id='s1'",
            (old_ts,),
        )
        conn.commit()
        conn.close()

        cancelled = q.expire_stale(older_than_seconds=600.0)
        assert "s1" in cancelled

        rows = q.list_all_with_status()
        s1 = next(r for r in rows if r["task_id"] == "s1")
        assert s1["status"] == "cancelled"

    def test_expire_stale_leaves_fresh_tasks_alone(self, tmp_db):
        """expire_stale does not cancel recently-active tasks."""
        q = TaskQueue(tmp_db)
        q.enqueue(TaskRequest(
            task_id="fresh", task_type="implement",
            description="fresh task", branch="main",
        ))
        conn = sqlite3.connect(tmp_db)
        # In_progress but last_activity just 60s ago
        conn.execute(
            "UPDATE tasks SET status='in_progress', last_activity_at=? WHERE task_id='fresh'",
            (time.time() - 60,),
        )
        conn.commit()
        conn.close()

        cancelled = q.expire_stale(older_than_seconds=600.0)
        assert "fresh" not in cancelled

    def test_expire_stale_via_http_endpoint(self, tmp_db):
        """POST /tasks/expire-stale returns the cancelled task list."""
        app, _ = _make_app(tmp_db)
        q = TaskQueue(tmp_db)
        q.enqueue(TaskRequest(
            task_id="ep1", task_type="implement",
            description="endpoint task", branch="main",
        ))
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "UPDATE tasks SET status='in_progress', last_activity_at=? WHERE task_id='ep1'",
            (time.time() - 1200,),
        )
        conn.commit()
        conn.close()

        with TestClient(app) as client:
            resp = client.post("/tasks/expire-stale?older_than=600")
        assert resp.status_code == 200
        body = resp.json()
        assert "ep1" in body["cancelled"]


# ---------------------------------------------------------------------------
# #158 — Gemini pane crash: task push falls into bash
# The fix: before pushing, detect bare bash prompt (no agent CLI running).
# If detected, requeue the task instead of pushing into bash.
# ---------------------------------------------------------------------------

class TestBashPromptDetectionOnDispatch:
    def test_task_not_pushed_when_pane_shows_bash_prompt(self, tmp_db):
        """
        If the target pane shows a bare bash prompt (agent CLI crashed),
        the task should NOT be pushed and should be requeued (still pending).
        """
        app, push_calls = _make_app(tmp_db)

        # Claude pane (%C) is showing a bash prompt (gemini/claude crashed)
        def pane_has_bash_prompt(pane_id: str) -> bool:
            return pane_id == "%C"

        with TestClient(app) as client, \
             patch("agent_crew.server._pane_has_bash_prompt", pane_has_bash_prompt):
            client.post("/tasks", json=_impl_payload("bp1"))

        # Push should NOT have gone to %C
        assert not any(pane == "%C" for pane, _ in push_calls), (
            "task must NOT be pushed to a pane showing a bash prompt"
        )

        # Task should be requeued (pending)
        rows = TaskQueue(tmp_db).list_all_with_status()
        bp1 = next((r for r in rows if r["task_id"] == "bp1"), None)
        assert bp1 is not None
        assert bp1["status"] == "pending", (
            "task should remain pending when target pane shows bash prompt"
        )

    def test_task_pushed_normally_when_pane_healthy(self, tmp_db):
        """Regression: when pane is healthy (no bash prompt), task is pushed normally."""
        app, push_calls = _make_app(tmp_db)

        with TestClient(app) as client, \
             patch("agent_crew.server._pane_has_bash_prompt", return_value=False):
            client.post("/tasks", json=_impl_payload("bp2"))

        assert any(pane == "%C" for pane, _ in push_calls), (
            "task should be pushed when pane is healthy"
        )


# ---------------------------------------------------------------------------
# #159 — crew run hangs forever when agents busy
# The fix: when the task is still pending (not picked up) after --timeout,
# print an informative message and exit WITHOUT auto-failing the task.
# ---------------------------------------------------------------------------

class TestCrewRunTimeoutPendingTask:
    def test_get_task_status_method_returns_correct_value(self, tmp_db):
        """TaskQueue.get_task_status returns the DB status for a task."""
        q = TaskQueue(tmp_db)
        q.enqueue(TaskRequest(
            task_id="gs1", task_type="implement",
            description="status check", branch="main",
        ))
        assert q.get_task_status("gs1") == "pending"
        assert q.get_task_status("nonexistent") is None

    def test_get_task_status_after_dequeue(self, tmp_db):
        """get_task_status returns in_progress after dequeue."""
        q = TaskQueue(tmp_db)
        q.enqueue(TaskRequest(
            task_id="gs2", task_type="implement",
            description="dequeue check", branch="main",
        ))
        q.dequeue(role="implementer")
        assert q.get_task_status("gs2") == "in_progress"

    def test_wait_exits_gracefully_when_task_still_pending(self, tmp_db, tmp_path):
        """
        When crew run --timeout N expires and the task is still pending
        (no agent picked it up), the tool must exit WITHOUT auto-failing the
        task. The task should remain in 'pending' state so a later agent can
        pick it up. Output must contain 'not picked up'.
        """
        from click.testing import CliRunner
        from agent_crew.cli import crew

        # Create minimal project state
        proj_dir = tmp_path / "myproj"
        proj_dir.mkdir()
        state = {
            "project": "myproj",
            "port": 0,
            "port_file": str(proj_dir / "port"),
            "session": "test",
            "window": "0",
            "pane_ids": [],      # no panes → first_pane_target = ""
            "pane_map": {},
            "agents": [],
            "worktrees": {},
            "db": tmp_db,
            "server_pid": 0,
            "sessions_file": str(proj_dir / "sessions.json"),
        }
        (proj_dir / "state.json").write_text(json.dumps(state))

        q = TaskQueue(tmp_db)

        # Pre-create the task as pending (simulate enqueue_implement)
        q.enqueue(TaskRequest(
            task_id="run-pending-1", task_type="implement",
            description="test pending", branch="main",
        ))

        runner = CliRunner()
        with patch("agent_crew.cli._port_listening", return_value=False), \
             patch("agent_crew.cli._pane_alive", return_value=True), \
             patch("agent_crew.cli._verify_delivery", return_value=False), \
             patch("agent_crew.loop.enqueue_implement", return_value="run-pending-1"), \
             patch("agent_crew.cli._auto_detect_project", return_value=None):

            result = runner.invoke(
                crew,
                [
                    "run", "test pending",
                    "--project", "myproj",
                    "--base", str(tmp_path),
                    "--timeout", "1",
                    "--no-tester",
                ],
                catch_exceptions=True,
            )

        # Key assertions:
        # 1. Task must remain pending (not auto-failed)
        status = q.get_task_status("run-pending-1")
        assert status == "pending", (
            f"task must stay pending when timed out before pickup (got: {status!r}). "
            "crew run must not auto-fail a task that was never picked up."
        )

        # 2. Output must indicate "not picked up" (not a hard error about timed-out)
        assert "not picked up" in result.output, (
            f"expected 'not picked up' message in output, got: {result.output!r}"
        )

        # 3. Exit code 0 (graceful stop, not a failure)
        assert result.exit_code == 0, (
            f"expected exit code 0 for graceful pending-task timeout, got {result.exit_code}"
        )
