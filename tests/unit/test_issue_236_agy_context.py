"""#236 — bound agy tester context growth, stop futile subscriber-lag retries.

#232 established the causal chain: the tester runs `agy -p --continue`
unconditionally, so its conversation grows without bound (alpha_engine
reached ~30k steps / 137 MB); every call re-sends that and burns quota;
agy hits 429; its internal retry stalls the agent_state pubsub subscriber;
agy kills the subscriber and surfaces only `subscriber fell behind
updates`. agent_crew classifies that surfaced line as a generic transient
and retries it 3 more times — measured 0/414 recoveries.

Two independent fixes, tested separately:

  1. cap the resumed agy conversation, and make the provider's resume flag
     follow Agent Crew's own context policy instead of always resuming;
  2. when a 429 can be correlated in agy's own log, report the failure as
     what it is (`agy_quota_exhausted`, already non-retriable) instead of
     the mask.
"""

import os
import time

import pytest

from agent_crew.server import (
    _detect_transient_error_in_log,
    agy_context_exceeds_cap,
    agy_conversation_size,
    agy_quota_correlated,
)


# ── fixtures: a fake agy home ─────────────────────────────────────────


@pytest.fixture
def agy_home(tmp_path):
    (tmp_path / "antigravity-cli" / "cache").mkdir(parents=True)
    (tmp_path / "antigravity-cli" / "conversations").mkdir(parents=True)
    (tmp_path / "antigravity-cli" / "log").mkdir(parents=True)
    return tmp_path


def _map_cwd(agy_home, cwd, conv_id):
    import json
    p = agy_home / "antigravity-cli" / "cache" / "last_conversations.json"
    data = json.loads(p.read_text()) if p.exists() else {}
    data[cwd] = conv_id
    p.write_text(json.dumps(data))


def _conversation(agy_home, conv_id, mb, *, wal_mb=0):
    d = agy_home / "antigravity-cli" / "conversations"
    (d / f"{conv_id}.db").write_bytes(b"\0" * int(mb * 1024 * 1024))
    if wal_mb:
        (d / f"{conv_id}.db-wal").write_bytes(b"\0" * int(wal_mb * 1024 * 1024))


# ── 1. sizing the resumed conversation ────────────────────────────────


def test_size_maps_cwd_to_its_conversation(agy_home):
    """agy keeps a plain {cwd: conversation_id} map; that is the join key."""
    _map_cwd(agy_home, "/wt/gemini", "conv-a")
    _conversation(agy_home, "conv-a", mb=3)

    size, conv = agy_conversation_size("/wt/gemini", home=agy_home)

    assert conv == "conv-a"
    assert size == 3 * 1024 * 1024


def test_size_includes_wal(agy_home):
    """⛔The -wal file is live conversation bytes too; ignoring it
    under-reports a conversation that is actively being written."""
    _map_cwd(agy_home, "/wt/gemini", "conv-b")
    _conversation(agy_home, "conv-b", mb=2, wal_mb=5)

    size, _ = agy_conversation_size("/wt/gemini", home=agy_home)

    assert size == 7 * 1024 * 1024


def test_unmapped_cwd_reports_zero(agy_home):
    assert agy_conversation_size("/wt/never-seen", home=agy_home) == (0, "")


def test_missing_agy_home_is_not_an_error(tmp_path):
    """A host without agy installed must not break dispatch."""
    assert agy_conversation_size("/wt/gemini", home=tmp_path / "nope") == (0, "")


# ── 2. the cap decision ───────────────────────────────────────────────


def test_oversized_conversation_trips_the_cap(agy_home):
    """★The alpha_engine shape: 137 MB resumed every dispatch."""
    _map_cwd(agy_home, "/wt/gemini", "conv-big")
    _conversation(agy_home, "conv-big", mb=137)

    over, info = agy_context_exceeds_cap("/wt/gemini", max_mb=64, home=agy_home)

    assert over is True
    assert info["conversation_id"] == "conv-big"
    assert info["bytes"] == 137 * 1024 * 1024
    assert info["cap_mb"] == 64


def test_small_conversation_keeps_resuming(agy_home):
    """⛔Continuity is the default — the cap is an exception, not a policy
    of starting fresh every time."""
    _map_cwd(agy_home, "/wt/gemini", "conv-small")
    _conversation(agy_home, "conv-small", mb=2)

    over, _ = agy_context_exceeds_cap("/wt/gemini", max_mb=64, home=agy_home)
    assert over is False


def test_cap_is_never_tripped_by_an_absent_conversation(agy_home):
    over, info = agy_context_exceeds_cap("/wt/gemini", max_mb=64, home=agy_home)
    assert over is False
    assert info["bytes"] == 0


def test_cap_read_failure_defaults_to_resuming(agy_home, monkeypatch):
    """⛔Fail-soft: if we cannot measure, do not force a reset. Wrongly
    resetting throws away a healthy conversation; wrongly resuming is the
    status quo the cap merely bounds."""
    def boom(*a, **k):
        raise OSError("disk gone")
    monkeypatch.setattr("agent_crew.server.agy_conversation_size", boom)

    over, _ = agy_context_exceeds_cap("/wt/gemini", max_mb=64, home=agy_home)
    assert over is False


def test_cap_disabled_by_zero(agy_home):
    """An operator must be able to turn the cap off entirely."""
    _map_cwd(agy_home, "/wt/gemini", "conv-big")
    _conversation(agy_home, "conv-big", mb=500)

    over, _ = agy_context_exceeds_cap("/wt/gemini", max_mb=0, home=agy_home)
    assert over is False


# ── 3. quota correlation behind the mask ──────────────────────────────


def _agy_log(agy_home, name, body, mtime=None):
    p = agy_home / "antigravity-cli" / "log" / name
    p.write_text(body)
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


_LAG = ("W pubsub.go:78] pubsub: Publish killing slow subscriber \"x\": "
        "subscriber fell behind updates, stalled for 6s\n")
_429 = ("I retry.go:44] GenerateFullResponseWithRetry: attempt 1 failed "
        "(RESOURCE_EXHAUSTED (code 429): Individual quota reached.)\n")


def test_quota_is_correlated_when_429_precedes_the_lag(agy_home):
    """★The 98%-of-the-time shape from #232: 429 first, lag kill after."""
    now = time.time()
    _agy_log(agy_home, "cli-1.log", _429 + _LAG, mtime=now)

    assert agy_quota_correlated(now - 60, now + 60, home=agy_home) is True


def test_no_correlation_when_the_log_has_no_429(agy_home):
    """⚠️The ~25% #232 could not attribute. These must stay distinguishable
    — we do not get to claim every lag event is a quota event."""
    now = time.time()
    _agy_log(agy_home, "cli-1.log", _LAG, mtime=now)

    assert agy_quota_correlated(now - 60, now + 60, home=agy_home) is False


def test_a_429_after_the_lag_does_not_count(agy_home):
    """⛔Order matters. A later, unrelated 429 is not this task's cause."""
    now = time.time()
    _agy_log(agy_home, "cli-1.log", _LAG + _429, mtime=now)

    assert agy_quota_correlated(now - 60, now + 60, home=agy_home) is False


def test_logs_outside_the_task_window_are_ignored(agy_home):
    """⛔Another task's quota failure must not be attributed to this one."""
    now = time.time()
    _agy_log(agy_home, "cli-old.log", _429 + _LAG, mtime=now - 86400)

    assert agy_quota_correlated(now - 60, now + 60, home=agy_home) is False


def test_correlation_is_fail_soft_without_agy_home(tmp_path):
    now = time.time()
    assert agy_quota_correlated(now - 60, now + 60, home=tmp_path / "nope") is False


# ── 4. the classifier keeps the distinction ───────────────────────────


def _dispatch_log(tmp_path, body):
    p = tmp_path / "dispatch_tester.log"
    p.write_text(body)
    return str(p)


def test_lag_without_correlation_stays_lag(tmp_path):
    """The retriable path is retained for the genuinely-unattributed case."""
    log = _dispatch_log(tmp_path,
        "Error: the connection to the agent was interrupted before the "
        "response finished: subscriber fell behind updates, stalled for 6s")
    assert _detect_transient_error_in_log(log) == "agy_subscriber_lag"


def test_surfaced_quota_still_wins_over_lag(tmp_path):
    """⛔Pre-existing precedence must not regress: when agy DOES surface the
    quota error, it is the cause regardless of any lag line."""
    log = _dispatch_log(tmp_path,
        "Error: Individual quota reached. Please upgrade your subscription.\n"
        "subscriber fell behind updates, stalled for 6s")
    assert _detect_transient_error_in_log(log) == "agy_quota_exhausted"


# ── 5. the cap event must mean the cap tripped (review-99ad8ad0) ──────
#
# The event was gated on `_agy_cap_info["bytes"] and _force_context_reset`.
# Both halves are wrong for this purpose: EVERY conversation has bytes, and
# `_force_context_reset` is also set by an operator's explicit
# task.context.context_reset. So an operator reset on a gemini worktree with
# a small conversation emitted `provider_context_capped` — mislabelling a
# deliberate reset as a size trip, and corrupting the exact signal #236 added
# the event to measure.


def _dispatch_and_collect_events(tmp_path, monkeypatch, *, task_context,
                                 conversation_mb, cap_mb=64):
    """Run one real dispatch and return the context lifecycle events."""
    import asyncio
    import json as _json

    from fastapi.testclient import TestClient

    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue
    from agent_crew.server import create_app

    wt = tmp_path / "gemini"
    wt.mkdir()
    (wt / ".git").mkdir()
    state_file = tmp_path / "state.json"
    state_file.write_text(_json.dumps({"worktrees": {"gemini": str(wt)}}))
    db = str(tmp_path / "t.db")
    events_path = tmp_path / "context_events.jsonl"

    import agent_crew.server as _srv

    monkeypatch.setattr(
        _srv, "agy_context_exceeds_cap",
        lambda cwd, *a, **k: (
            conversation_mb > cap_mb,
            {"bytes": int(conversation_mb * 1024 * 1024),
             "conversation_id": "conv-test", "cap_mb": cap_mb},
        ))

    async def _fake_exec(*cmd, **kwargs):
        class _P:
            returncode = 0
            pid = 1

            async def communicate(self):
                return (b"", b"")

            async def wait(self):
                return 0

        return _P()

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec",
                        _fake_exec)

    app = create_app(db_path=db, pane_map={}, port=0, state_path=str(state_file),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        q = TaskQueue(db)
        q.enqueue(TaskRequest(task_id="cap-1", task_type="test",
                              description="d", branch="main",
                              context=task_context))
        task = q.dequeue(role="tester")
        assert task is not None
        asyncio.run(app.state.dispatch_task(task, "tester"))

    if not events_path.exists():
        return []
    return [_json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]


def _types(events):
    return [e.get("event_type") or e.get("type") for e in events]


def test_explicit_operator_reset_below_the_cap_is_not_reported_as_capped(
    tmp_path, monkeypatch,
):
    """★The regression: a deliberate reset must not be labelled a size trip."""
    events = _dispatch_and_collect_events(
        tmp_path, monkeypatch,
        task_context={"context_reset": True},   # operator asked for it
        conversation_mb=2,                       # well under the cap
    )

    # A first dispatch into a (project, agent, worktree) mints generation 1,
    # which the existing code reports as `context_created`; a later forced
    # reset reports `context_reset`. Either means a fresh context was minted,
    # which is the property that matters here.
    assert {"context_created", "context_reset"} & set(_types(events)), \
        f"the operator's reset should still mint a context: {_types(events)}"
    assert "provider_context_capped" not in _types(events), (
        "an explicit reset below the cap was mislabelled as a cap trip:\n"
        f"{_types(events)}")


def test_cap_trip_still_emits_the_capped_event(tmp_path, monkeypatch):
    """⛔The gate must not silence the real signal it exists to carry."""
    events = _dispatch_and_collect_events(
        tmp_path, monkeypatch,
        task_context={},                 # no operator reset
        conversation_mb=137,             # the alpha_engine shape
    )

    assert "provider_context_capped" in _types(events), _types(events)
    capped = next(e for e in events
                  if (e.get("event_type") or e.get("type")) == "provider_context_capped")
    assert capped["bytes"] == 137 * 1024 * 1024
    assert capped["conversation_id"] == "conv-test"
    assert capped["cap_mb"] == 64


def test_no_reset_and_under_the_cap_emits_nothing_capped(tmp_path, monkeypatch):
    events = _dispatch_and_collect_events(
        tmp_path, monkeypatch, task_context={}, conversation_mb=2)

    assert "provider_context_capped" not in _types(events)


def test_operator_reset_AND_a_real_cap_trip_still_reports_capped(
    tmp_path, monkeypatch,
):
    """Both true at once: the cap genuinely tripped, so say so."""
    events = _dispatch_and_collect_events(
        tmp_path, monkeypatch,
        task_context={"context_reset": True}, conversation_mb=137)

    assert "provider_context_capped" in _types(events), _types(events)
