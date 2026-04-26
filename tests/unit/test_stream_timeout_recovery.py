"""Stream-timeout recovery tests (Issue #85).

Three behaviours pinned here:

1. The watchdog reminder message instructs the agent on three options
   (complete / failed / keep working), the second of which is the explicit
   stream-timeout recovery path.
2. The fallback rate-limit detector recognises watchdog auto-fail summaries
   so a stuck pane reroutes to the next agent in the chain.
3. End-to-end: a watchdog timeout produces a `fallback-*` task assigned to
   the next agent in the chain (not a same-role retry).
"""
from fastapi.testclient import TestClient

from agent_crew.fallback import has_rate_limit_signal, is_rate_limit_error
from agent_crew.queue import TaskQueue
from agent_crew.server import _format_reminder_message, create_app


# ---------------------------------------------------------------------------
# Reminder-message contract
# ---------------------------------------------------------------------------


class TestReminderMessage:
    def test_reminder_contains_three_options(self):
        msg = _format_reminder_message("t-101", port=8200, idle_seconds=400)
        assert "FINISHED" in msg
        assert "STREAM/API TIMEOUT" in msg
        assert "STILL WORKING" in msg

    def test_reminder_includes_failed_status_curl(self):
        """Path 2 must hand the agent a copy-pasteable POST that submits
        status="failed" so the fallback policy can pick up the pieces."""
        msg = _format_reminder_message("t-101", port=8200, idle_seconds=400)
        assert '"status":"failed"' in msg
        assert "API stream timeout" in msg

    def test_reminder_includes_completed_status_curl(self):
        msg = _format_reminder_message("t-101", port=8200, idle_seconds=400)
        assert '"status":"completed"' in msg

    def test_reminder_carries_task_id_and_port(self):
        msg = _format_reminder_message("t-uniqueid", port=9123, idle_seconds=315.4)
        assert "t-uniqueid" in msg
        assert "9123" in msg
        # Idle seconds rendered as integer (no fractional)
        assert "315s" in msg or "315 s" in msg or "315" in msg


# ---------------------------------------------------------------------------
# Fallback pattern recognition
# ---------------------------------------------------------------------------


class TestFalloverPatternsExtended:
    def test_watchdog_timeout_summary_matches(self):
        """The summary the watchdog writes when force-failing a stuck task."""
        summary = "watchdog timeout: pane idle 920s without sign of activity (threshold 900s)"
        assert is_rate_limit_error(summary) is True
        assert has_rate_limit_signal(summary, []) is True

    def test_stream_idle_timeout_matches(self):
        assert is_rate_limit_error("API Stream idle timeout - partial response received") is True

    def test_partial_response_matches(self):
        assert is_rate_limit_error("partial response, no recovery") is True

    def test_pane_idle_matches(self):
        assert is_rate_limit_error("pane idle for far too long") is True

    def test_unrelated_summary_still_excluded(self):
        # Sanity check — the new patterns shouldn't accidentally widen the net.
        assert is_rate_limit_error("agent posted PR #42") is False
        assert is_rate_limit_error("merge conflict on requirements.txt") is False


# ---------------------------------------------------------------------------
# End-to-end: watchdog timeout triggers fallback chain
# ---------------------------------------------------------------------------


def _task_payload(task_id="t1", task_type="implement"):
    return {
        "task_id": task_id,
        "task_type": task_type,
        "description": "do work",
        "branch": "main",
        "priority": 3,
        "context": {},
        "project": "",
    }


class _Idle:
    """pane_busy_fn that always reports idle — forces watchdog to time out."""

    def __call__(self, pane_id: str) -> bool:
        return False


class _Push:
    def __init__(self):
        self.calls: list = []

    def __call__(self, pane_id, text):
        self.calls.append((pane_id, text))


def test_watchdog_timeout_routes_to_next_agent_via_fallback(tmp_db):
    """Stuck task auto-failed by the watchdog must produce a `fallback-*`
    task assigned to the next agent in the chain, not a same-role retry."""
    push = _Push()
    panes = {
        "implementer": "%C", "claude": "%C",
        "reviewer": "%X", "codex": "%X",
        "tester": "%G", "gemini": "%G",
    }
    app = create_app(
        db_path=tmp_db,
        pane_map=panes,
        port=8200,
        push_fn=push,
        pane_busy_fn=_Idle(),
        reminder_seconds=300.0,
        timeout_seconds=900.0,
        watchdog_disabled=True,  # drive ticks manually
    )
    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("impl-stream"))
        # Force last_activity_at to a known value, then tick past timeout.
        TaskQueue(tmp_db).bump_activity("impl-stream", ts=1000.0)
        result = app.state.watchdog_tick(now=2500.0)

    assert result["timed_out"] == ["impl-stream"]

    # A fallback task should now exist routed to codex (the next agent in
    # the default implement chain).
    tasks = TaskQueue(tmp_db).list_tasks()
    fallback = [t for t in tasks if t.task_id.startswith("fallback-impl-stream-")]
    assert len(fallback) == 1
    assert fallback[0].context["agent_override"] == "codex"
    assert fallback[0].context["fallback_excluded"] == ["claude"]
    # And no same-role retry was enqueued (the watchdog path doesn't go
    # through `_auto_retry_failed_task` once the fallback handler returns True).
    assert [t for t in tasks if t.task_id.startswith("retry-impl-stream-")] == []
