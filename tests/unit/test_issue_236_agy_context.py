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
