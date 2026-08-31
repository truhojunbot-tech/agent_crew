"""#231 — the pane-idle watchdog must not auto-fail a quiet-but-alive agent.

`crew run`'s `_wait` loop auto-failed a task after a fixed 300s of no tmux
capture change, with no check on whether the agent was actually still
running. Two things went wrong in production:

  * #224 `impl-f9525bd7` — fired while the implementer waited on a
    backgrounded pytest suite. Real work was lost.
  * #224 `impl-a399c7e1` — fired during a `sleep`-polling wait; the agent
    was still alive and later submitted a real `completed` result over the
    top of the watchdog's false `failed`.

Both are the same defect: silence was treated as death. A full test suite
is legitimately silent for minutes, so the fixed window was simply below
the length of normal work.

The fix is two independent parts, tested separately here:

  1. the fail window scales with the caller's own `wait_timeout` instead of
     being a fixed 300s regardless of role;
  2. before auto-failing, the pane's foreground process decides — a shell
     prompt means the agent really died, an agent process means it is
     merely quiet.
"""

from unittest.mock import MagicMock, patch

import pytest

from agent_crew.cli import (
    IDLE_FAIL_FLOOR_SECONDS,
    IDLE_FAIL_TIMEOUT_RATIO,
    agent_liveness,
    idle_thresholds,
    should_auto_fail_idle,
)


# ── 1. the window scales with the role's own timeout ──────────────────


class TestIdleThresholds:
    def test_long_implement_timeout_gets_a_long_window(self):
        """★#214 gives the implementer 1800s; a 300s idle window inside it
        is what made a 5-minute pytest run look like death."""
        warn, fail = idle_thresholds(1800.0)
        assert fail == 900.0
        assert warn < fail

    def test_short_timeout_falls_back_to_the_floor(self):
        """⛔Never scale *below* the old behaviour — a short wait keeps the
        original 300s so queue cleanup does not get slower."""
        _warn, fail = idle_thresholds(60.0)
        assert fail == IDLE_FAIL_FLOOR_SECONDS == 300.0

    def test_warning_precedes_the_failure(self):
        for timeout in (60.0, 600.0, 1800.0, 7200.0):
            warn, fail = idle_thresholds(timeout)
            assert 0 < warn < fail, timeout

    def test_ratio_is_applied_above_the_floor(self):
        _warn, fail = idle_thresholds(4000.0)
        assert fail == pytest.approx(4000.0 * IDLE_FAIL_TIMEOUT_RATIO)

    def test_zero_or_negative_timeout_is_safe(self):
        for bad in (0.0, -1.0):
            warn, fail = idle_thresholds(bad)
            assert fail == IDLE_FAIL_FLOOR_SECONDS
            assert 0 < warn < fail


# ── 2. liveness decides, not silence ──────────────────────────────────


def _cmd(name: str, *, returncode: int = 0) -> MagicMock:
    return MagicMock(stdout=name + "\n", returncode=returncode)


class TestAgentLiveness:
    @pytest.mark.parametrize("shell", ["bash", "sh", "zsh", "fish", "dash"])
    def test_shell_prompt_means_the_agent_died(self, shell):
        """★The canonical crash signature from #195 — the CLI exited and the
        pane fell back to its shell."""
        with patch("agent_crew.cli.subprocess.run", return_value=_cmd(shell)):
            assert agent_liveness("%1") == "dead"

    @pytest.mark.parametrize("proc", ["claude", "node", "codex", "agy", "python3"])
    def test_agent_process_means_quiet_not_dead(self, proc):
        with patch("agent_crew.cli.subprocess.run", return_value=_cmd(proc)):
            assert agent_liveness("%1") == "alive"

    def test_tmux_failure_is_unknown_not_a_verdict(self):
        with patch("agent_crew.cli.subprocess.run",
                   return_value=_cmd("", returncode=1)):
            assert agent_liveness("%1") == "unknown"

    def test_empty_pane_target_is_unknown(self):
        assert agent_liveness("") == "unknown"

    def test_unrecognised_command_counts_as_alive(self):
        """⛔Conservative: something we do not recognise is more likely a
        wrapper than a corpse, and guessing 'dead' destroys real work."""
        with patch("agent_crew.cli.subprocess.run", return_value=_cmd("uv")):
            assert agent_liveness("%1") == "alive"


# ── 3. the policy that consumes the two above ─────────────────────────


class TestShouldAutoFailIdle:
    def test_alive_agent_is_never_auto_failed(self):
        """★The whole point of #231. A quiet agent keeps its task; the
        caller's own `wait_timeout` still bounds it, so a genuinely hung
        agent cannot wait forever."""
        assert should_auto_fail_idle("alive") is False

    def test_dead_agent_is_auto_failed(self):
        """Auto-fail stays correct where it was always correct."""
        assert should_auto_fail_idle("dead") is True

    def test_unknown_preserves_the_previous_behaviour(self):
        """⚠️Deliberate: when tmux cannot tell us, keep failing as before.
        Flipping this to 'never fail' would let a genuinely dead agent hold
        the queue for the entire wait_timeout on any tmux hiccup."""
        assert should_auto_fail_idle("unknown") is True

    def test_policy_covers_every_liveness_value(self):
        for value in ("alive", "dead", "unknown"):
            assert isinstance(should_auto_fail_idle(value), bool)


# ── 4. the two halves compose the way the loop uses them ──────────────


class TestComposition:
    def test_quiet_pytest_run_under_a_long_timeout_survives(self):
        """★The exact production scenario: a 1748s implement task goes
        silent for ~6 minutes running the suite."""
        _warn, fail = idle_thresholds(1800.0)
        idle_for = 360.0  # a full pytest run
        assert idle_for < fail, "a 6-minute suite must not even reach the window"

        # And even past the window, a live agent is not failed.
        with patch("agent_crew.cli.subprocess.run", return_value=_cmd("claude")):
            assert should_auto_fail_idle(agent_liveness("%1")) is False

    def test_crashed_agent_is_still_reaped(self):
        """⛔The fix must not make genuine crashes invisible."""
        with patch("agent_crew.cli.subprocess.run", return_value=_cmd("bash")):
            assert should_auto_fail_idle(agent_liveness("%1")) is True


# ── 5. server-side watchdog: extend for a live agent, don't disable ───
#
# The dispatcher's own watchdog has the same defect, but it cannot use the
# CLI's fix verbatim: `crew run`'s loop is bounded by wait_timeout, so
# "alive -> never fail" is still bounded there. The server watchdog IS the
# bound for pane-based tasks, so refusing to ever fail a live-but-hung
# agent would strand it forever. A live agent therefore gets a longer
# leash, not an unlimited one.

from fastapi.testclient import TestClient  # noqa: E402

from agent_crew.server import create_app  # noqa: E402


class _Liveness:
    def __init__(self, verdict="alive"):
        self.verdict = verdict

    def __call__(self, pane_id):
        return self.verdict


def _app(tmp_db, liveness, *, timeout=900.0, multiplier=3.0):
    return create_app(
        db_path=tmp_db,
        pane_map={"implementer": "%100"},
        port=8100,
        push_fn=lambda pane_id, text: None,
        pane_busy_fn=lambda pane_id: False,   # always looks idle
        pane_liveness_fn=liveness,
        reminder_seconds=300.0,
        timeout_seconds=timeout,
        alive_timeout_multiplier=multiplier,
        watchdog_disabled=True,
    )


def _payload(task_id):
    return {"task_id": task_id, "task_type": "implement",
            "description": "quiet work", "branch": "main", "priority": 3,
            "context": {}}


def _drive(app, tmp_db, task_id, *, tick_at):
    """Pin the idle clock at t=1000, land the required reminder at t=1400,
    then take the deciding tick at ``tick_at``."""
    from agent_crew.queue import TaskQueue

    with TestClient(app) as client:
        client.post("/tasks", json=_payload(task_id))
        TaskQueue(tmp_db).bump_activity(task_id, ts=1000.0)
        app.state.watchdog_tick(now=1400.0)      # idle 400s -> reminder
        return app.state.watchdog_tick(now=tick_at)


# idle at t=2000 is 1000s: past the 900s base threshold, under the 2700s
# leash a live agent gets. That gap is the whole fix.
_PAST_BASE = 2000.0
_PAST_LEASH = 4000.0      # idle 3000s > 900 * 3


def test_live_agent_is_not_timed_out_at_the_normal_threshold(tmp_db):
    """★A live agent quiet past the 900s threshold keeps its task — the
    server-side version of the #224 work loss."""
    result = _drive(_app(tmp_db, _Liveness("alive")), tmp_db, "t-alive",
                    tick_at=_PAST_BASE)
    assert result["timed_out"] == []


def test_live_agent_is_still_timed_out_eventually(tmp_db):
    """⛔The leash is longer, not infinite — a hung-but-alive agent must
    still be reaped, because nothing else bounds a pane-based task."""
    result = _drive(_app(tmp_db, _Liveness("alive")), tmp_db, "t-hung",
                    tick_at=_PAST_LEASH)
    assert result["timed_out"] == ["t-hung"]


def test_dead_agent_times_out_at_the_normal_threshold(tmp_db):
    """Unchanged where it was always correct."""
    result = _drive(_app(tmp_db, _Liveness("dead")), tmp_db, "t-dead",
                    tick_at=_PAST_BASE)
    assert result["timed_out"] == ["t-dead"]


def test_unknown_liveness_keeps_the_previous_behaviour(tmp_db):
    result = _drive(_app(tmp_db, _Liveness("unknown")), tmp_db, "t-unknown",
                    tick_at=_PAST_BASE)
    assert result["timed_out"] == ["t-unknown"]


def test_liveness_probe_failure_does_not_break_the_tick(tmp_db):
    """⛔A raising probe must not take the watchdog down."""
    def boom(pane_id):
        raise RuntimeError("tmux exploded")

    result = _drive(_app(tmp_db, boom), tmp_db, "t-boom", tick_at=_PAST_BASE)
    # Falls back to the previous behaviour rather than crashing.
    assert result["timed_out"] == ["t-boom"]
