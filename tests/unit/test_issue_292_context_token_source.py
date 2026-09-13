"""#292 — the auto-clear was measuring a hint that is usually not on screen.

`_pane_token_count` scraped `tmux capture-pane` for Claude Code's
"/clear to save Xk tokens" footer. No `-S`, so only the visible screen; and the
hint is only rendered in some UI states. When it was absent the regex found
nothing and the function returned **0**, which reads as "well under threshold"
rather than "unknown" — so the 200,000 auto-clear silently never fired.

Measured on this host, 2026-09-13, with the threshold at 200,000:

    project            pane   hint?   transcript tokens   verdict
    agent_council      %22    no                 62,025   under
    agent_crew         -      n/a               786,552   OVER THRESHOLD
    alpha_engine       -      n/a               664,792   OVER THRESHOLD
    halla              -      n/a               231,590   OVER THRESHOLD
    quota-core         -      n/a               575,398   OVER THRESHOLD
    quota-ops          -      n/a               606,702   OVER THRESHOLD

Five of seven worktrees over the ceiling this mechanism exists to enforce, and
neither readable pane was showing the hint. Same anti-pattern as #260's
file-size gap: a check that looks active because it fires occasionally, while
providing no ceiling at all.

The fix is the ground truth #284 already reads — the pane's own session
transcript — with the pane hint kept only as a fallback for providers that have
no transcript, and `None` finally distinguished from `0`.
"""

import json
import re
from unittest.mock import patch

import pytest

from agent_crew import server as sv


def _pane(text):
    class _R:
        returncode = 0
        stdout = text
        stderr = ""
    return lambda *a, **k: _R()


def _session(home, cwd, usage):
    d = home / "projects" / re.sub(r"[/._]", "-", str(cwd))
    d.mkdir(parents=True, exist_ok=True)
    (d / "s.jsonl").write_text(
        json.dumps({"type": "assistant", "message": {"usage": usage}}) + "\n")


HINT = "  new task? /clear to save 544.1k tokens\n"
NO_HINT = "  [Edit] /tmp/wt/tests/unit/test_thing.py\n  → 41L: # ...\n"


# ── 1. absent hint is unknown, not zero ───────────────────────────────


def test_a_missing_hint_is_unknown_not_zero():
    """★★The bug. `0` reads as "well under threshold" to every caller."""
    with patch.object(sv.subprocess, "run", _pane(NO_HINT)):
        assert sv._pane_token_count("%1") is None


def test_a_present_hint_is_still_parsed():
    with patch.object(sv.subprocess, "run", _pane(HINT)):
        assert sv._pane_token_count("%1") == 544_100


def test_a_pane_that_cannot_be_read_is_unknown():
    def boom(*a, **k):
        raise OSError("no tmux")
    with patch.object(sv.subprocess, "run", boom):
        assert sv._pane_token_count("%1") is None


# ── 2. the transcript is the ground truth ─────────────────────────────


def test_the_transcript_is_preferred_over_the_pane(tmp_path):
    """★★A pane showing no hint while the session holds 900k is the reported
    situation exactly; the transcript has to win."""
    _session(tmp_path, "/w/claude", {"cache_read_input_tokens": 900_000})
    with patch.object(sv.subprocess, "run", _pane(NO_HINT)):
        tokens, source = sv._context_token_count("%1", "/w/claude", home=tmp_path)
    assert tokens == 900_000 and source == "transcript"


def test_the_transcript_wins_even_when_the_hint_is_present(tmp_path):
    """⛔The hint is a rounded UI string; the transcript is the number that gets
    re-billed. When both exist they are not interchangeable, and the exact one
    is the one to threshold against."""
    _session(tmp_path, "/w/claude", {"cache_read_input_tokens": 123_456})
    with patch.object(sv.subprocess, "run", _pane(HINT)):
        tokens, source = sv._context_token_count("%1", "/w/claude", home=tmp_path)
    assert tokens == 123_456 and source == "transcript"


def test_a_measured_zero_from_the_transcript_is_not_unknown(tmp_path):
    """⛔#288's contract, carried through: a genuinely empty window is a
    measurement. It must not fall through to the pane hint as if nothing had
    been read."""
    _session(tmp_path, "/w/claude", {"cache_read_input_tokens": 0,
                                     "cache_creation_input_tokens": 0,
                                     "input_tokens": 0})
    with patch.object(sv.subprocess, "run", _pane(HINT)):
        tokens, source = sv._context_token_count("%1", "/w/claude", home=tmp_path)
    assert tokens == 0 and source == "transcript"


def test_without_a_transcript_the_pane_hint_is_the_fallback(tmp_path):
    """Providers other than Claude have no transcript to read; the old signal
    is still better than nothing for them."""
    with patch.object(sv.subprocess, "run", _pane(HINT)):
        tokens, source = sv._context_token_count("%1", "", home=tmp_path)
    assert tokens == 544_100 and source == "pane_hint"


def test_neither_source_is_unknown(tmp_path):
    with patch.object(sv.subprocess, "run", _pane(NO_HINT)):
        tokens, source = sv._context_token_count("%1", "/w/nothing", home=tmp_path)
    assert tokens is None and source == "unknown"


# ── 3. what the caller does with it ───────────────────────────────────


def _decide(tokens):
    """The clear decision, isolated from the push plumbing."""
    return sv._should_clear_context(tokens, threshold=200_000)


def test_over_the_threshold_clears():
    assert _decide(200_001) is True


def test_exactly_the_threshold_clears():
    """The existing contract is `>=`; keep it."""
    assert _decide(200_000) is True


def test_under_the_threshold_does_not():
    assert _decide(199_999) is False


def test_a_measured_zero_does_not_clear():
    assert _decide(0) is False


def test_unknown_does_not_clear_but_is_not_zero():
    """⛔The asymmetry that matters. Unknown must not clear — a forced reset on
    a reading nobody took throws away a working context — but it must also not
    be silently indistinguishable from a small measurement, which is precisely
    how this went unnoticed. The caller logs it; the decision is False."""
    assert _decide(None) is False


# ── 4. the real push paths ────────────────────────────────────────────


def test_the_push_path_clears_on_a_saturated_transcript(tmp_path, monkeypatch):
    """★★End to end: a pane with no hint whose session is over the ceiling now
    gets cleared, which is the behaviour #292 says never happened."""
    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    wt = tmp_path / "worktrees" / "demo" / "claude"
    wt.mkdir(parents=True)
    _session(tmp_path / "claudehome", str(wt), {"cache_read_input_tokens": 900_000})
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"port": 0, "worktrees": {"claude": str(wt)}}))

    cleared = []
    monkeypatch.setattr(sv, "_claude_home", lambda home=None: tmp_path / "claudehome")
    monkeypatch.setattr(sv, "_pane_clear_context", lambda pane: cleared.append(pane))
    monkeypatch.setattr(sv, "_pane_alive_for_push", lambda pane: True)
    monkeypatch.setattr(sv, "_pane_has_usage_limit", lambda pane: False)
    monkeypatch.setattr(sv, "_pane_dismiss_permission_prompt", lambda pane: None)
    monkeypatch.setattr(sv.subprocess, "run", _pane(NO_HINT))
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")

    db = str(tmp_path / "tasks.db")
    pushed = []
    app = create_app(db_path=db, state_path=str(state),
                     pane_map={"implementer": "%1"}, port=0,
                     push_fn=lambda pane, text: pushed.append(pane),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        # Posted through the real endpoint: enqueueing is what triggers the
        # push path, so this exercises the trigger rather than a helper.
        client.post("/tasks", json={
            "task_id": "t-292", "task_type": "implement", "description": "go",
            "branch": "main", "priority": 3, "context": {}, "project": "demo"})

    assert cleared == ["%1"], "a saturated pane was pushed to without clearing"
    assert pushed == ["%1"]


def test_the_push_path_does_not_clear_a_small_session(tmp_path, monkeypatch):
    """⛔The control. Clearing on every push would destroy the cache-hit
    behaviour the threshold exists to preserve."""
    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    wt = tmp_path / "worktrees" / "demo" / "claude"
    wt.mkdir(parents=True)
    _session(tmp_path / "claudehome", str(wt), {"cache_read_input_tokens": 1_000})
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"port": 0, "worktrees": {"claude": str(wt)}}))

    cleared = []
    monkeypatch.setattr(sv, "_claude_home", lambda home=None: tmp_path / "claudehome")
    monkeypatch.setattr(sv, "_pane_clear_context", lambda pane: cleared.append(pane))
    monkeypatch.setattr(sv, "_pane_alive_for_push", lambda pane: True)
    monkeypatch.setattr(sv, "_pane_has_usage_limit", lambda pane: False)
    monkeypatch.setattr(sv, "_pane_dismiss_permission_prompt", lambda pane: None)
    monkeypatch.setattr(sv.subprocess, "run", _pane(NO_HINT))
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")

    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, state_path=str(state),
                     pane_map={"implementer": "%1"}, port=0,
                     push_fn=lambda pane, text: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        client.post("/tasks", json={
            "task_id": "t-292b", "task_type": "implement", "description": "go",
            "branch": "main", "priority": 3, "context": {}, "project": "demo"})

    assert cleared == []
