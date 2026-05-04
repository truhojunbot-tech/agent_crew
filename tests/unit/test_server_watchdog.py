"""Unit tests for the heartbeat / inactivity watchdog (issue #76).

The watchdog runs as a background asyncio task in production. These tests
drive the synchronous tick directly via ``app.state.watchdog_tick`` so we
don't have to deal with timing.
"""
from fastapi.testclient import TestClient

from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _task_payload(task_id="t1", task_type="implement", description="do work", priority=3, project=""):
    return {
        "task_id": task_id,
        "task_type": task_type,
        "description": description,
        "branch": "main",
        "priority": priority,
        "context": {},
        "project": project,
    }


def _result_payload(task_id, status="completed", summary="done"):
    return {
        "task_id": task_id,
        "status": status,
        "summary": summary,
        "verdict": None,
        "findings": [],
        "pr_number": None,
    }


class _PaneState:
    """Toggleable pane busy/idle state keyed by pane_id."""

    def __init__(self):
        self.busy: dict[str, bool] = {}

    def set_busy(self, pane_id: str, busy: bool) -> None:
        self.busy[pane_id] = busy

    def __call__(self, pane_id: str) -> bool:
        return self.busy.get(pane_id, False)


class _RecordingPush:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, pane_id: str, text: str) -> None:
        self.calls.append((pane_id, text))


def _make_app(tmp_db, *, panes, busy_fn, push_fn,
              reminder=300.0, timeout=900.0):
    return create_app(
        db_path=tmp_db,
        pane_map=panes,
        port=8100,
        push_fn=push_fn,
        pane_busy_fn=busy_fn,
        reminder_seconds=reminder,
        timeout_seconds=timeout,
        watchdog_disabled=True,  # we drive ticks manually
    )


# U-WD01: Busy pane → last_activity_at refreshed, no reminder/timeout fired.
def test_u_wd01_busy_pane_bumps_activity(tmp_db):
    busy = _PaneState()
    push = _RecordingPush()
    app = _make_app(tmp_db, panes={"implementer": "%100"}, busy_fn=busy, push_fn=push)

    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("t-busy"))
        # First push fires synchronously on enqueue → 1 push call so far.
        assert len(push.calls) == 1

        # Pane is busy → tick should bump activity, NOT send reminder/timeout.
        busy.set_busy("%100", True)
        # Run the tick well past both thresholds — busy state must short-circuit.
        result = app.state.watchdog_tick(now=10_000.0)

    assert result["bumped"] == ["t-busy"]
    assert result["reminded"] == []
    assert result["timed_out"] == []
    # Activity row updated to "now" so a subsequent idle tick starts the clock fresh.
    rows = TaskQueue(tmp_db).list_in_progress_with_activity()
    assert rows and rows[0]["last_activity_at"] == 10_000.0
    # No extra push (no reminder).
    assert len(push.calls) == 1


# U-WD02: Idle pane below the reminder threshold → no action.
def test_u_wd02_idle_within_threshold_noop(tmp_db):
    busy = _PaneState()
    push = _RecordingPush()
    app = _make_app(tmp_db, panes={"implementer": "%100"},
                    busy_fn=busy, push_fn=push,
                    reminder=300.0, timeout=900.0)

    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("t-young"))
        # Force a known last_activity_at so we control idle_for precisely.
        TaskQueue(tmp_db).bump_activity("t-young", ts=1000.0)

        # 100s elapsed (< reminder 300s) → no reminder/timeout.
        result = app.state.watchdog_tick(now=1100.0)

    assert result == {"bumped": [], "reminded": [], "timed_out": []}
    # Only the original task push is recorded — no nudge.
    assert len(push.calls) == 1


# U-WD03: Idle ≥ reminder threshold → exactly one nudge pushed; subsequent
# ticks at the same idle level don't spam.
def test_u_wd03_idle_past_reminder_pushes_once(tmp_db):
    busy = _PaneState()
    push = _RecordingPush()
    app = _make_app(tmp_db, panes={"implementer": "%100"},
                    busy_fn=busy, push_fn=push,
                    reminder=300.0, timeout=900.0)

    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("t-idle"))
        TaskQueue(tmp_db).bump_activity("t-idle", ts=1000.0)
        # idle_for = 400s — past reminder, well under timeout.
        first = app.state.watchdog_tick(now=1400.0)
        second = app.state.watchdog_tick(now=1500.0)

    assert first["reminded"] == ["t-idle"]
    assert second["reminded"] == []
    # Reminder push counted — original + 1 nudge = 2 calls.
    assert len(push.calls) == 2
    nudge_text = push.calls[1][1]
    assert "AGENT_CREW REMINDER" in nudge_text
    assert "t-idle" in nudge_text


# U-WD04: A busy heartbeat between two idle ticks resets the dedupe set, so
# the next idle stretch can earn a fresh reminder.
def test_u_wd04_reminder_dedupe_resets_after_busy(tmp_db):
    busy = _PaneState()
    push = _RecordingPush()
    app = _make_app(tmp_db, panes={"implementer": "%100"},
                    busy_fn=busy, push_fn=push,
                    reminder=300.0, timeout=10_000.0)

    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("t-flap"))
        TaskQueue(tmp_db).bump_activity("t-flap", ts=1000.0)

        app.state.watchdog_tick(now=1400.0)  # idle 400s → reminder fires
        busy.set_busy("%100", True)
        app.state.watchdog_tick(now=1500.0)  # busy → bump, drop from dedupe
        busy.set_busy("%100", False)
        # Activity is now 1500.0 → idle_for = 1900-1500 = 400s past reminder again.
        result = app.state.watchdog_tick(now=1900.0)

    assert result["reminded"] == ["t-flap"]
    # 2 reminders total + 1 original push = 3.
    assert len(push.calls) == 3


# U-WD05: Idle ≥ timeout → task auto-failed, role released, queued task pushed.
# Timeout requires a prior reminder (#152: dispatch-grace window).
def test_u_wd05_idle_past_timeout_auto_fails_and_pushes_next(tmp_db):
    busy = _PaneState()
    push = _RecordingPush()
    app = _make_app(tmp_db, panes={"implementer": "%100"},
                    busy_fn=busy, push_fn=push,
                    reminder=300.0, timeout=900.0)

    with TestClient(app) as client:
        # First task — pushed immediately, marked in_progress.
        client.post("/tasks", json=_task_payload("t-stuck"))
        # Second task — queued behind t-stuck because impl role is busy.
        client.post("/tasks", json=_task_payload("t-next"))
        assert len(push.calls) == 1, "second task should have been queued"

        TaskQueue(tmp_db).bump_activity("t-stuck", ts=1000.0)
        # First: fire reminder tick (idle_for=400s ≥ reminder=300s).
        app.state.watchdog_tick(now=1400.0)
        # Then: fire timeout tick (idle_for=1500s ≥ timeout=900s).
        result = app.state.watchdog_tick(now=2500.0)

    assert result["timed_out"] == ["t-stuck"]
    # Stuck task should now be 'failed' in the DB. list_tasks returns
    # TaskRequests without status, so verify via list_all_with_status.
    all_with_status = TaskQueue(tmp_db).list_all_with_status()
    stuck_status = next(r["status"] for r in all_with_status if r["task_id"] == "t-stuck")
    assert stuck_status == "failed"
    # push.calls: initial t-stuck + reminder nudge + t-next dispatch
    # (#152: timeout requires a prior reminder, so reminder fires at tick 1)
    assert len(push.calls) == 3
    assert "t-next" in push.calls[2][1]


# U-WD06: discuss tasks → watchdog uses context.agent for pane resolution.
def test_u_wd06_resolves_discuss_pane_via_agent(tmp_db):
    busy = _PaneState()
    push = _RecordingPush()
    panes = {"panel": "%999", "claude": "%CLA", "codex": "%COD"}
    app = _make_app(tmp_db, panes=panes, busy_fn=busy, push_fn=push,
                    reminder=300.0, timeout=900.0)

    with TestClient(app) as client:
        payload = _task_payload("d-claude", task_type="discuss",
                                description="Topic X")
        payload["context"] = {"agent": "claude"}
        client.post("/tasks", json=payload)
        TaskQueue(tmp_db).bump_activity("d-claude", ts=1000.0)
        # Mark only the *correct* pane busy — if resolution were broken the
        # watchdog would treat this task as idle.
        busy.set_busy("%CLA", True)
        result = app.state.watchdog_tick(now=1500.0)

    assert result["bumped"] == ["d-claude"]


# U-WD06b: Background loop actually fires ticks while the app is alive and
# cancels cleanly on shutdown — covers the asyncio plumbing the unit-tick
# tests skip via watchdog_disabled=True.
def test_u_wd06b_async_loop_runs_and_cancels(tmp_db):
    busy = _PaneState()
    push = _RecordingPush()
    app = create_app(
        db_path=tmp_db,
        pane_map={"implementer": "%100"},
        port=8100,
        push_fn=push,
        pane_busy_fn=busy,
        reminder_seconds=300.0,
        timeout_seconds=10_000.0,
        watchdog_interval=0.05,   # tight loop so the test is quick
        watchdog_disabled=False,
    )
    import time as _t

    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("t-loop"))
        TaskQueue(tmp_db).bump_activity("t-loop", ts=_t.time() - 1000.0)
        busy.set_busy("%100", True)
        # Give the loop a couple of ticks to bump activity.
        _t.sleep(0.25)
    # If we reach here without hanging, lifespan cancelled the loop on exit.
    rows = TaskQueue(tmp_db).list_in_progress_with_activity()
    assert rows
    assert rows[0]["last_activity_at"] >= _t.time() - 5.0


# U-WD07: pane_busy_fn raising must not crash the tick — broken pane probes
# shouldn't take down the whole server.
def test_u_wd07_pane_busy_fn_exception_swallowed(tmp_db):
    push = _RecordingPush()

    def angry_busy_fn(_pane_id: str) -> bool:
        raise RuntimeError("tmux exploded")

    app = _make_app(tmp_db, panes={"implementer": "%100"},
                    busy_fn=angry_busy_fn, push_fn=push,
                    reminder=300.0, timeout=900.0)

    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("t-err"))
        TaskQueue(tmp_db).bump_activity("t-err", ts=1000.0)
        # Should return cleanly; nothing bumped/reminded/timed_out for this task.
        result = app.state.watchdog_tick(now=2500.0)

    assert result == {"bumped": [], "reminded": [], "timed_out": []}


# U-WD08: Watchdog timeout sends C-c to hung pane before routing fallback (#148).
def test_u_wd08_timeout_sends_ctrl_c_to_pane(tmp_db, monkeypatch):
    """After force_fail, watchdog must send C-c to the hung pane so child
    processes (e.g. `gh pr view`) are killed and the CLI can receive the
    fallback task."""
    import subprocess as _sp
    busy = _PaneState()
    push = _RecordingPush()
    app = _make_app(tmp_db, panes={"implementer": "%100"},
                    busy_fn=busy, push_fn=push,
                    reminder=300.0, timeout=900.0)

    sent_keys = []

    def fake_run(cmd, **kwargs):
        if "send-keys" in cmd:
            sent_keys.append(cmd)
        result = _sp.CompletedProcess(cmd, 0, stdout="", stderr="")
        return result

    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("t-hung"))
        TaskQueue(tmp_db).bump_activity("t-hung", ts=1000.0)
        # First tick: send reminder (idle_for=400s ≥ reminder=300s).
        app.state.watchdog_tick(now=1400.0)
        monkeypatch.setattr("agent_crew.server.subprocess.run", fake_run)
        # Second tick: timeout fires (idle_for=1500s ≥ timeout=900s).
        app.state.watchdog_tick(now=2500.0)

    # Must have sent C-c to the hung pane
    ctrl_c_calls = [c for c in sent_keys if "C-c" in c]
    assert ctrl_c_calls, "watchdog must send C-c to hung pane on timeout"
    assert any("%100" in " ".join(c) for c in ctrl_c_calls)
