"""#1037: per-provider open-review cap.

All reviews on a crew share one reviewer session (8114: one ChatGPT thread).
Nothing limited how many were open at once, so a burst of auto-enqueued
reviews queued behind it until the 43200 s timeout (11-18: 5 timed_out, 18
cancelled in 24 h). ``AGENT_CREW_REVIEW_OPEN_CAP`` parks the excess in the
#1033 ``waiting_pool`` state under ``REVIEW_OPEN_CAP`` and resumes FIFO,
exactly once, when a review of that provider reaches a terminal state.
"""

import logging
import time

import pytest
from fastapi.testclient import TestClient

from agent_crew import pool_signal
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app
from tests.unit.test_issue_1033_pool_hold import (  # noqa: F401 (notices: fixture)
    _events, _signal, _state, _wait, notices)

ROLES = {"implementer": "claude", "reviewer": "codex", "tester": "script"}
CAP = pool_signal.REVIEW_OPEN_CAP_REASON


def _review(queue, task_id, *, auto=True, agent_override=None, priority=3, branch="main"):
    ctx = {"risk_tier": 1}
    if auto:
        ctx["prev_task_id"] = f"impl-{task_id}"
    if agent_override:
        ctx["agent_override"] = agent_override
    queue.enqueue(TaskRequest(task_id, "review", f"review {task_id}", branch=branch,
                              priority=priority, project="agent_crew", context=ctx))
    time.sleep(0.005)   # distinct created_at: FIFO is by creation order
    return task_id


def _statuses(queue, *ids):
    return [queue.get_task_status(t) for t in ids]


def _cap_app(tmp_path, monkeypatch, cap, roles=ROLES, *, loop=False):
    """App with the cap set. ``loop=False`` keeps the dispatcher loop off so a
    test drives ``app.state.apply_review_open_cap()`` itself (the sweep needs
    the started app for its queue); ``loop=True`` runs the real tick."""
    if cap is not None:
        monkeypatch.setenv(pool_signal.REVIEW_OPEN_CAP_ENV, str(cap))
    else:
        monkeypatch.delenv(pool_signal.REVIEW_OPEN_CAP_ENV, raising=False)
    _signal(tmp_path, monkeypatch, "open")
    # Same app as test_issue_1033_pool_hold._app, but the dispatcher switch is
    # read inside create_app, so it has to be set before the app is built.
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1" if loop else "0")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", "0.02")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    app = create_app(queue._db_path, state_path=_state(tmp_path, roles), project="agent_crew",
                     pane_map={}, watchdog_disabled=True, anomaly_disabled=True)
    dispatched = []

    async def fake_dispatch(task, role, **kw):
        dispatched.append(task.task_id)

    app.state.dispatch_task = fake_dispatch
    return app, queue, dispatched


# ── env parsing (same idiom as claude_cloud.cloud_max_concurrency) ─────────

def test_unset_or_empty_cap_is_no_cap(monkeypatch):
    monkeypatch.delenv(pool_signal.REVIEW_OPEN_CAP_ENV, raising=False)
    assert pool_signal.review_open_cap() is None
    monkeypatch.setenv(pool_signal.REVIEW_OPEN_CAP_ENV, "  ")
    assert pool_signal.review_open_cap() is None


def test_valid_cap_parses(monkeypatch):
    monkeypatch.setenv(pool_signal.REVIEW_OPEN_CAP_ENV, " 2 ")
    assert pool_signal.review_open_cap() == 2


@pytest.mark.parametrize("raw", ["abc", "0", "-3", "1.5"])
def test_bad_cap_logs_and_falls_back_to_no_cap(monkeypatch, caplog, raw):
    monkeypatch.setenv(pool_signal.REVIEW_OPEN_CAP_ENV, raw)
    with caplog.at_level(logging.WARNING, logger="agent_crew.pool_signal"):
        assert pool_signal.review_open_cap() is None
    assert any(pool_signal.REVIEW_OPEN_CAP_ENV in r.getMessage() for r in caplog.records)


# ── sweep: park over the cap ──────────────────────────────────────────────

def test_cap_2_with_3_auto_reviews_parks_one_with_one_notice(tmp_path, monkeypatch, notices):
    app, queue, _ = _cap_app(tmp_path, monkeypatch, 2)
    with TestClient(app):
        r1, r2, r3 = (_review(queue, f"review-{i}") for i in (1, 2, 3))
        out = app.state.apply_review_open_cap()
        assert out["cap"] == 2 and out["parked"] == [r3] and out["resumed"] == []
        assert _statuses(queue, r1, r2, r3) == ["pending", "pending", "waiting_pool"]
        parked = _events(queue, r3, "pool_hold_parked")
        assert len(parked) == 1
        assert parked[0]["code"] == CAP and parked[0]["pool"] == "codex"
        assert parked[0]["rule"] == pool_signal.REVIEW_OPEN_CAP_RULE
        assert "codex open reviews 3 > cap 2" in parked[0]["reason"]
        assert len(notices) == 1 and notices[0][:2] == (r3, "review_open_cap_parked")
        assert CAP in notices[0][2]
        # Idempotent: a second sweep neither parks more nor re-notices.
        assert app.state.apply_review_open_cap() == {"cap": 2, "parked": [], "resumed": []}
        assert len(notices) == 1 and len(_events(queue, r3, "pool_hold_parked")) == 1


def test_unset_cap_is_unchanged_behaviour(tmp_path, monkeypatch, notices):
    app, queue, _ = _cap_app(tmp_path, monkeypatch, None)
    with TestClient(app):
        ids = [_review(queue, f"review-{i}") for i in range(1, 6)]
        assert app.state.apply_review_open_cap() == {"cap": None, "parked": [], "resumed": []}
        assert _statuses(queue, *ids) == ["pending"] * 5
        assert notices == []


def test_manual_review_is_counted_but_never_parked(tmp_path, monkeypatch, notices):
    app, queue, _ = _cap_app(tmp_path, monkeypatch, 1)
    with TestClient(app):
        manual = _review(queue, "review-manual", auto=False)
        auto = _review(queue, "review-auto")
        # 2 open > cap 1: the manual row holds its place, the auto row parks.
        assert app.state.apply_review_open_cap()["parked"] == [auto]
        assert _statuses(queue, manual, auto) == ["pending", "waiting_pool"]
        # Only manual rows over the cap: nothing to park, and nothing fails.
        _review(queue, "review-manual-2", auto=False, branch="feature/other")
        assert app.state.apply_review_open_cap()["parked"] == []


def test_only_the_named_provider_is_counted(tmp_path, monkeypatch, notices):
    app, queue, _ = _cap_app(tmp_path, monkeypatch, 2)
    with TestClient(app):
        codex = [_review(queue, f"review-codex-{i}") for i in (1, 2)]
        other = [_review(queue, f"review-claude-{i}", agent_override="claude") for i in (1, 2, 3)]
        out = app.state.apply_review_open_cap()
        # codex: 2 open, at the cap, none parked. claude: 3 open, newest parked.
        assert out["parked"] == [other[2]]
        assert _statuses(queue, *codex) == ["pending", "pending"]
        assert _statuses(queue, *other) == ["pending", "pending", "waiting_pool"]
        assert _events(queue, other[2], "pool_hold_parked")[0]["pool"] == "claude"


# ── sweep: resume FIFO, exactly once ──────────────────────────────────────

def test_terminal_review_resumes_the_parked_one_exactly_once(tmp_path, monkeypatch, notices):
    app, queue, _ = _cap_app(tmp_path, monkeypatch, 2)
    with TestClient(app):
        r1, r2, r3 = (_review(queue, f"review-{i}") for i in (1, 2, 3))
        assert app.state.apply_review_open_cap()["parked"] == [r3]
        assert queue.cancel(r1)   # one review of the provider reaches a terminal state
        out = app.state.apply_review_open_cap()
        assert out == {"cap": 2, "parked": [], "resumed": [r3]}
        assert _statuses(queue, r2, r3) == ["pending", "pending"]
        resumed = _events(queue, r3, "pool_hold_resumed")
        assert len(resumed) == 1 and resumed[0]["rule"] == pool_signal.REVIEW_OPEN_CAP_RULE
        assert "codex open reviews 1 < cap 2" in resumed[0]["reason"]
        # Exactly once: nothing left to resume, nothing re-parked.
        assert app.state.apply_review_open_cap() == {"cap": 2, "parked": [], "resumed": []}
        assert len(_events(queue, r3, "pool_hold_resumed")) == 1
        assert len(_events(queue, r3, "pool_hold_parked")) == 1


def test_resume_is_fifo_oldest_parked_first(tmp_path, monkeypatch, notices):
    app, queue, _ = _cap_app(tmp_path, monkeypatch, 1)
    with TestClient(app):
        r1, r2, r3 = (_review(queue, f"review-{i}") for i in (1, 2, 3))
        assert app.state.apply_review_open_cap()["parked"] == [r3, r2]
        assert _statuses(queue, r1, r2, r3) == ["pending", "waiting_pool", "waiting_pool"]
        assert queue.cancel(r1)
        assert app.state.apply_review_open_cap()["resumed"] == [r2]
        assert _statuses(queue, r2, r3) == ["pending", "waiting_pool"]
        assert queue.cancel(r2)
        assert app.state.apply_review_open_cap()["resumed"] == [r3]
        assert _statuses(queue, r3) == ["pending"]


def test_resume_counts_only_the_same_provider(tmp_path, monkeypatch, notices):
    app, queue, _ = _cap_app(tmp_path, monkeypatch, 1)
    with TestClient(app):
        codex1, codex2 = _review(queue, "review-codex-1"), _review(queue, "review-codex-2")
        gem1 = _review(queue, "review-claude-1", agent_override="claude")
        assert app.state.apply_review_open_cap()["parked"] == [codex2]
        assert queue.cancel(gem1)   # another provider's terminal state frees nothing here
        assert app.state.apply_review_open_cap()["resumed"] == []
        assert _statuses(queue, codex1, codex2) == ["pending", "waiting_pool"]


# ── #1033 pool hold on the same row still wins ────────────────────────────

def test_pool_hold_park_is_not_resumed_by_the_cap(tmp_path, monkeypatch, notices):
    app, queue, _ = _cap_app(tmp_path, monkeypatch, 2)
    with TestClient(app):
        held = _review(queue, "review-held")
        assert queue.park_pool_hold(held, reason="hold slow projection", rule="hold",
                                    code="POOL_HOLD", pool="claude")
        # Plenty of room under the cap, yet the POOL_HOLD park is not ours to lift.
        assert app.state.apply_review_open_cap() == {"cap": 2, "parked": [], "resumed": []}
        assert queue.get_task_status(held) == "waiting_pool"
        assert _events(queue, held, "pool_hold_resumed") == []


def test_pool_sweep_does_not_resume_a_cap_park(tmp_path, monkeypatch, notices):
    app, queue, _ = _cap_app(tmp_path, monkeypatch, 1)
    with TestClient(app):
        r1, r2 = _review(queue, "review-1"), _review(queue, "review-2")
        assert app.state.apply_review_open_cap()["parked"] == [r2]
        # The Claude pool signal is OPEN; before #1037 the pool sweep resumed
        # every waiting_pool row under the Claude pool. The cap park stays.
        out = app.state.apply_pool_signal()
        assert out["rule"] == "open" and out["resumed"] == []
        assert queue.get_task_status(r2) == "waiting_pool"
        held = _review(queue, "review-held")
        assert queue.park_pool_hold(held, reason="hold", rule="hold", code="POOL_HOLD")
        # A POOL_HOLD park still resumes on open, as before.
        assert app.state.apply_pool_signal()["resumed"] == [held]
        assert queue.get_task_status(r2) == "waiting_pool"


def test_parked_row_rows_reader_filters_on_latest_park_code(tmp_path, monkeypatch):
    app, queue, _ = _cap_app(tmp_path, monkeypatch, 1)
    with TestClient(app):
        a, b = _review(queue, "review-a"), _review(queue, "review-b")
        assert queue.park_pool_hold(a, reason="r", rule="hold", code="POOL_HOLD")
        assert queue.park_pool_hold(b, reason="r", rule=pool_signal.REVIEW_OPEN_CAP_RULE, code=CAP)
        assert [t.task_id for t in queue.waiting_pool_rows_by_park_code(CAP)] == [b]
        assert [t.task_id for t in queue.waiting_pool_rows_by_park_code("POOL_HOLD")] == [a]
        assert queue.resume_pool_hold(b, rule=pool_signal.REVIEW_OPEN_CAP_RULE)
        assert queue.waiting_pool_rows_by_park_code(CAP) == []


# ── dispatcher tick integration ───────────────────────────────────────────

def test_dispatcher_tick_parks_then_resumes_on_terminal(tmp_path, monkeypatch, notices):
    app, queue, dispatched = _cap_app(tmp_path, monkeypatch, 2, loop=True)
    r1, r2, r3 = (_review(queue, f"review-{i}") for i in (1, 2, 3))
    with TestClient(app):
        assert _wait(lambda: queue.get_task_status(r3) == "waiting_pool")
        assert _wait(lambda: sorted(dispatched) == [r1, r2])
        time.sleep(0.2)
        assert r3 not in dispatched
        assert queue.cancel(r1)
        assert _wait(lambda: r3 in dispatched)
    assert dispatched.count(r3) == 1
    assert len(_events(queue, r3, "pool_hold_parked")) == 1
    assert len(_events(queue, r3, "pool_hold_resumed")) == 1
    assert [n[:2] for n in notices] == [(r3, "review_open_cap_parked")]
