"""Regression tests for `_pane_is_busy` (Issue #84 — diff-based detection).

The default busy-probe used to scrape the pane for specific banner strings
(``✻``, ``Crunched``, ``esc to interrupt``). Both versions broke when the
banner text changed (past-tense scrollback or future Claude UI updates).
The current implementation compares pane content across consecutive calls,
which is text-agnostic.

These tests pin that contract: identical captures → idle, different
captures → busy, with the cache deterministically reset between cases.
"""
from unittest.mock import MagicMock, patch

import pytest

from agent_crew.server import _pane_is_busy, _reset_pane_busy_cache


@pytest.fixture(autouse=True)
def _isolate_busy_cache():
    _reset_pane_busy_cache()
    yield
    _reset_pane_busy_cache()


def _captured(text: str, *, returncode: int = 0) -> MagicMock:
    m = MagicMock()
    m.stdout = text
    m.returncode = returncode
    return m


def test_first_call_for_pane_returns_false():
    """No prior snapshot → assume idle (activity is fresh from task enqueue)."""
    cap = _captured("anything\n")
    with patch("agent_crew.server.subprocess.run", return_value=cap):
        assert _pane_is_busy("%999") is False


def test_unchanged_capture_returns_false():
    """Two identical captures back-to-back → pane is idle."""
    cap = _captured("the agent finished — same content twice\n")
    with patch("agent_crew.server.subprocess.run", return_value=cap):
        assert _pane_is_busy("%999") is False  # priming
        assert _pane_is_busy("%999") is False


def test_changed_capture_returns_true():
    """When the second capture differs from the first → pane is busy."""
    captures = iter(
        [
            _captured("step 1: thinking…\n"),
            _captured("step 1: thinking…\nstep 2: writing code\n"),
        ]
    )

    def fake_run(*_args, **_kwargs):
        return next(captures)

    with patch("agent_crew.server.subprocess.run", side_effect=fake_run):
        assert _pane_is_busy("%999") is False  # priming
        assert _pane_is_busy("%999") is True


def test_past_tense_glyph_does_not_trip_busy():
    """Issue #84 root cause — Claude leaves ``✻ Cogitated for 5m 16s`` in
    scrollback after work finishes. As long as the pane content is stable,
    the diff probe must report idle."""
    cap = _captured(
        "✻ Cogitated for 5m 16s\n"
        "❯ \n"
        " ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
    )
    with patch("agent_crew.server.subprocess.run", return_value=cap):
        # Three ticks at the watchdog interval, all reading the same scrollback
        # → pane stays idle even with the past-tense glyph visible.
        assert _pane_is_busy("%999") is False
        assert _pane_is_busy("%999") is False
        assert _pane_is_busy("%999") is False


def test_each_pane_tracked_independently():
    """One pane changing does not flip another pane's idle state."""
    pane_a_cap = _captured("a-1\n")
    pane_b_caps = iter([_captured("b-1\n"), _captured("b-2\n")])

    def fake_run(args, **_kwargs):
        # args = ['tmux', 'capture-pane', '-p', '-t', '%pane']
        target = args[-1]
        if target == "%A":
            return pane_a_cap
        return next(pane_b_caps)

    with patch("agent_crew.server.subprocess.run", side_effect=fake_run):
        assert _pane_is_busy("%A") is False  # priming A
        assert _pane_is_busy("%B") is False  # priming B
        assert _pane_is_busy("%A") is False  # A unchanged
        assert _pane_is_busy("%B") is True   # B changed b-1 → b-2


def test_subprocess_failure_returns_false():
    """tmux failure must not crash the watchdog tick."""
    failing = _captured("", returncode=1)
    with patch("agent_crew.server.subprocess.run", return_value=failing):
        assert _pane_is_busy("%999") is False


def test_subprocess_exception_returns_false():
    """A real exception (e.g. tmux binary missing) must also be swallowed."""
    with patch("agent_crew.server.subprocess.run", side_effect=FileNotFoundError("tmux")):
        assert _pane_is_busy("%999") is False


def test_reset_cache_makes_next_call_act_like_first():
    cap_a = _captured("a\n")
    with patch("agent_crew.server.subprocess.run", return_value=cap_a):
        assert _pane_is_busy("%999") is False  # priming
        _reset_pane_busy_cache()
        # After reset the next call has no prior snapshot again.
        assert _pane_is_busy("%999") is False
