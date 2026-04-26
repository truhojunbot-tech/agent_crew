"""Regression tests for _pane_is_busy (Issue #84).

The watchdog (#76 / PR #77) relies on _pane_is_busy to decide whether to
bump last_activity_at. When the function over-reports busy, the idle clock
never starts and reminders / timeouts never fire — exactly the failure mode
documented in #84.
"""
from unittest.mock import MagicMock, patch

from agent_crew.server import _pane_is_busy


def _captured(text: str) -> MagicMock:
    """Build a MagicMock whose stdout matches what tmux capture-pane returns."""
    m = MagicMock()
    m.stdout = text
    m.returncode = 0
    return m


def test_active_pane_with_esc_to_interrupt_is_busy():
    pane = (
        "✻ Cogitating… (12s · ↓ 1.2k tokens)\n"
        "ℹ Working through the diff…\n"
        "─────────\n"
        "❯ \n"
        "─────────\n"
        " ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt\n"
    )
    with patch("agent_crew.server.subprocess.run", return_value=_captured(pane)):
        assert _pane_is_busy("%999") is True


def test_idle_pane_with_past_tense_glyph_is_not_busy():
    """Issue #84 root cause — ``✻ Cogitated for 5m 16s`` persists in scrollback
    after work finishes. The old implementation matched on ``✻`` literally and
    returned True forever; the watchdog never fired."""
    pane = (
        "✻ Cogitated for 5m 16s\n"
        "─────────\n"
        "❯ \n"
        "─────────\n"
        " ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
    )
    with patch("agent_crew.server.subprocess.run", return_value=_captured(pane)):
        assert _pane_is_busy("%999") is False


def test_idle_pane_with_past_tense_crunched_is_not_busy():
    """Same root cause via the ``Crunched`` literal (e.g. ``Crunched for 30s``)."""
    pane = (
        "✻ Crunched for 30s\n"
        "❯ \n"
        " ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
    )
    with patch("agent_crew.server.subprocess.run", return_value=_captured(pane)):
        assert _pane_is_busy("%999") is False


def test_completely_blank_pane_is_not_busy():
    with patch("agent_crew.server.subprocess.run", return_value=_captured("\n")):
        assert _pane_is_busy("%999") is False


def test_codex_active_state_is_busy():
    """Codex shows ``Working (12s • esc to interrupt)`` mid-task."""
    pane = (
        "› Summarize recent commits\n"
        "◦ Working (12s • esc to interrupt)\n"
        "  gpt-5.4-mini medium · ~/.agent_crew/worktrees/agent_crew/codex\n"
    )
    with patch("agent_crew.server.subprocess.run", return_value=_captured(pane)):
        assert _pane_is_busy("%999") is True


def test_subprocess_failure_returns_false():
    """A broken tmux call must not crash the watchdog tick."""
    failing = MagicMock(stdout="", returncode=1)
    with patch("agent_crew.server.subprocess.run", return_value=failing):
        assert _pane_is_busy("%999") is False
