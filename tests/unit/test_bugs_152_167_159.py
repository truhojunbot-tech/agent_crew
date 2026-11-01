"""Tests for bugs #152, #167, #159.

#152 — watchdog measures idle_for from push_at (not dequeue time)
#167 — force_fail records error_info; fallback loop detection
#159 — pending task dispatched immediately when result submitted
"""
import json

import pytest
pytestmark = pytest.mark.usefixtures("legacy_gemini_tester_policy")
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


def _post_result(client, task_id, status="completed", summary="done", error_info=None):
    return client.post(f"/tasks/{task_id}/result", json={
        "task_id": task_id,
        "status": status,
        "summary": summary,
        "verdict": None,
        "findings": [],
        "pr_number": None,
        "error_info": error_info,
    })


class _RecordingPush:
    def __init__(self):
        self.calls: list = []

    def __call__(self, pane_id, text):
        self.calls.append((pane_id, text))


@pytest.mark.parametrize(
    ("task_type", "error_info", "retry_flag", "expected_retry"),
    [
        ("implement", None, None, False),
        ("implement", None, "1", False),
        ("implement", None, "0", False),
        ("implement", {"kind": "transient", "reason": "infrastructure_failure"}, "0", True),
        ("review", None, "0", True),
        ("test", None, "0", True),
    ],
)
def test_auto_retry_skips_agent_reported_implement_failure(
    tmp_db, tmp_path, monkeypatch, task_type, error_info, retry_flag, expected_retry,
):
    if retry_flag is None:
        monkeypatch.delenv("AGENT_CREW_RETRY_IMPLEMENT_SELF_FAILED", raising=False)
    else:
        monkeypatch.setenv("AGENT_CREW_RETRY_IMPLEMENT_SELF_FAILED", retry_flag)
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    # This test submits the result itself. The default script tester would
    # claim test tasks on a background thread and race that manual result.
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "project": "test", "role_agents": {
            "implementer": "codex", "reviewer": "claude", "tester": "gemini",
        },
    }))
    app = create_app(
        db_path=tmp_db, pane_map={"implementer": "%100"}, port=8100,
        push_fn=_RecordingPush(), watchdog_disabled=True, fallback_disabled=True,
        state_path=str(state_path), identity_required=False,
    )
    with TestClient(app) as client:
        assert _post_task(
            client, "self-report-probe", task_type=task_type,
            ctx={"pr_number": 123} if task_type == "review" else None,
        ).status_code == 201
        if task_type == "test":
            assert getattr(app.state, "script_test_thread", None) is None
        response = client.post("/tasks/self-report-probe/result", json={
            "task_id": "self-report-probe", "status": "failed",
            "summary": "agent failed", "error_info": error_info,
        })
        assert response.status_code == 200

    tasks = TaskQueue(tmp_db).list_tasks()
    retry = [task for task in tasks if task.task_id == "retry-self-report-probe-a1"]
    assert len(retry) == int(expected_retry)
    original = next(task for task in tasks if task.task_id == "self-report-probe")
    assert original.context.get("retry_attempt", 0) == 0
    assert original.error_info == error_info
    if expected_retry:
        assert retry[0].context["retry_attempt"] == 1


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



# ---------------------------------------------------------------------------
# #167 — error recording + fallback loop detection
# ---------------------------------------------------------------------------

class TestFallbackLoop:
    """Bug #167 error recording and retired fallback behavior under #308."""

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

    # #308 retired the provider fallback chain; no new depth is minted.
    def test_u_167_02_rate_limit_does_not_create_fallback_depth(self, tmp_db):
        """A provider limit never creates a cross-provider successor."""
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
        handled = auto_fallback_failed_task(
            q, "orig", result, "implement",
            pane_map={"implementer": "%91", "claude": "%91", "codex": "%92", "gemini": "%93"},
        )
        assert handled is False
        assert not any(t.task_id.startswith("fallback-") for t in q.list_tasks())

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
            pane_map={"implementer": "%91", "claude": "%91", "codex": "%92", "gemini": "%93"},
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



# ---------------------------------------------------------------------------
# Review feedback: wire error_info into real failure paths; cancel chain on loop
# ---------------------------------------------------------------------------

class TestErrorInfoWiring:
    """Review feedback for #167: error_info must be stored in ALL failure paths,
    not just the force_fail signature."""

    # U-167-05: watchdog force_fail wires structured error_info into DB.

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
    """Retired fallback behavior and the active retry's DB counter."""

    # #308 also disables legacy fallback rows from cancelling their root.
    def test_u_167_07_legacy_fallback_depth_does_not_cancel_root(self, tmp_db):
        """An old fallback row cannot revive cross-provider loop handling."""
        from agent_crew.pipeline import auto_fallback_failed_task
        from agent_crew.queue import _CEA_SYSTEM_SUCCESSOR_PROVENANCE

        q = TaskQueue(tmp_db)
        # A failed row is terminal under G12 and cannot be retroactively
        # cancelled; keep the root active to test the loop's cancel handoff.
        q.enqueue(_task("orig-root"))
        q.dequeue(role="implementer")

        # Simulate a fallback at depth=3 that traces back to orig-root.
        q.enqueue(_task("fb3", ctx={
            "fallback_chain_depth": 3,
            "agent_override": "gemini",
            "fallback_excluded": ["claude", "codex", "gemini"],
            "original_task_id": "orig-root",
        }), _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
        q.dequeue(role="implementer")
        result = TaskResult(
            task_id="fb3",
            status="failed",
            summary="usage limit hit",
            verdict=None,
            findings=[],
        )
        handled = auto_fallback_failed_task(
            q, "fb3", result, "implement",
            pane_map={"implementer": "%91", "claude": "%91", "codex": "%92", "gemini": "%93"},
        )
        assert handled is False

        pending = q.list_tasks(status="pending")
        assert len(pending) == 0, "No new pending task on loop detection"

        orig_tasks = [t for t in q.list_tasks() if t.task_id == "orig-root"]
        assert orig_tasks[0].status == "in_progress", (
            "retired fallback handling must not cancel the root"
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

pytestmark = [pytestmark, pytest.mark.usefixtures("pinned_task_transport")]
