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


# ---------------------------------------------------------------------------
# Thinking-marker fallback (#138) — static capture + live thinking indicator
# ---------------------------------------------------------------------------


from agent_crew.server import _pane_is_thinking  # noqa: E402


def test_pane_is_thinking_detects_esc_to_interrupt():
    """`esc to interrupt` in the bottom lines → True."""
    capture = (
        "Previous output line 1\n"
        "Previous output line 2\n"
        "✶ Quantumizing… (2m 40s · ↓ 6.2k tokens · almost done thinking)\n"
        " esc to interrupt\n"
    )
    assert _pane_is_thinking(capture) is True


def test_pane_is_thinking_detects_token_counter():
    """Token counter like '↓ 6.2k tokens' also fires."""
    capture = "doing stuff\n" * 20 + "↓ 6.2k tokens\n"
    assert _pane_is_thinking(capture) is True


def test_pane_is_thinking_past_tense_scrollback_not_triggered():
    """Past-tense thinking output scrolled away (beyond the tail window) must
    NOT fire the thinking marker (#84 regression guard).
    Thinking lines come first; 25 neutral lines push them above the 10-line tail.
    """
    old_thinking = "✶ Quantumizing… (2m 40s · ↓ 6.2k tokens)\nesc to interrupt\n"
    neutral_lines = "some completed output line\n" * 25
    current_bottom = "❯ \n ⏵⏵ bypass permissions on\n"
    capture = old_thinking + neutral_lines + current_bottom
    assert _pane_is_thinking(capture) is False


def test_pane_is_busy_returns_true_on_thinking_even_if_capture_unchanged(
    _isolate_busy_cache,
):
    """#138 regression: static capture + 'esc to interrupt' → busy regardless
    of whether there is a prior snapshot. Thinking markers fire immediately."""
    thinking_capture = (
        "✶ Quantumizing… (2m 40s · ↓ 6.2k tokens)\n"
        " esc to interrupt\n"
    )
    cap = _captured(thinking_capture)
    with patch("agent_crew.server.subprocess.run", return_value=cap):
        # First call: no prior snapshot, but thinking marker present → busy
        assert _pane_is_busy("%888") is True
        # Second call: same text + thinking marker → still busy
        assert _pane_is_busy("%888") is True


def test_pane_is_busy_returns_false_without_thinking_and_static(
    _isolate_busy_cache,
):
    """Static capture with no thinking markers → genuinely idle."""
    cap = _captured("❯ \n ⏵⏵ bypass permissions on\n")
    with patch("agent_crew.server.subprocess.run", return_value=cap):
        assert _pane_is_busy("%888") is False  # priming
        assert _pane_is_busy("%888") is False  # idle
