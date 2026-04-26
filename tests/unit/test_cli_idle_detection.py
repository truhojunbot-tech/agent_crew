"""Diff-based pane idle detection in the crew run wrapper (Issue #103)."""
from unittest.mock import MagicMock, patch

import pytest

from agent_crew.cli import (
    _pane_changed,
    _pane_looks_idle,
    _reset_pane_content_cache,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    _reset_pane_content_cache()
    yield
    _reset_pane_content_cache()


def _capture(stdout: str, *, returncode: int = 0) -> MagicMock:
    return MagicMock(stdout=stdout, returncode=returncode)


# ---------------------------------------------------------------------------
# _pane_looks_idle (kept for backward compatibility, last-line scan)
# ---------------------------------------------------------------------------


class TestPaneLooksIdle:
    def test_zsh_prompt_visible_is_idle(self):
        assert _pane_looks_idle("some output\n❯") is True

    def test_completed_keyword_is_idle(self):
        assert _pane_looks_idle("agent says: Completed") is True

    def test_active_pane_with_footer_is_not_idle_via_last_line(self):
        # The Claude Code multi-line footer makes the last line a non-prompt
        # banner. The last-line scan returns False in this case — that's the
        # exact bug that motivated #103. The diff probe is the real check.
        pane = (
            "✻ Cooked for 5m 4s\n"
            "❯\n"
            " ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
        )
        assert _pane_looks_idle(pane) is False


# ---------------------------------------------------------------------------
# _pane_changed (diff-based, the new probe)
# ---------------------------------------------------------------------------


class TestPaneChanged:
    def test_first_call_returns_false(self):
        """No prior baseline — return False (idle). Mirrors the server-side
        `_pane_is_busy` convention so callers can use both interchangeably."""
        with patch("agent_crew.cli.subprocess.run", return_value=_capture("any\n")):
            assert _pane_changed("%999") is False

    def test_unchanged_capture_returns_false(self):
        with patch("agent_crew.cli.subprocess.run", return_value=_capture("same content\n")):
            assert _pane_changed("%999") is False  # priming
            assert _pane_changed("%999") is False  # unchanged → idle

    def test_changed_capture_returns_true(self):
        captures = iter([_capture("step 1\n"), _capture("step 1\nstep 2\n")])
        with patch("agent_crew.cli.subprocess.run", side_effect=lambda *a, **k: next(captures)):
            assert _pane_changed("%999") is False  # priming
            assert _pane_changed("%999") is True   # changed → busy

    def test_past_tense_glyph_does_not_trip_busy(self):
        """The exact scenario from #103: pane shows ``Cooked for 5m 4s`` and
        sits there, capture content stable across ticks → must report idle."""
        cap = _capture(
            "✻ Cooked for 5m 4s\n"
            "❯\n"
            " ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
        )
        with patch("agent_crew.cli.subprocess.run", return_value=cap):
            assert _pane_changed("%999") is False  # priming
            # Multiple subsequent ticks — all stable, so all idle.
            assert _pane_changed("%999") is False
            assert _pane_changed("%999") is False
            assert _pane_changed("%999") is False

    def test_independent_per_pane_state(self):
        captures = {
            "%A": iter([_capture("a-1\n"), _capture("a-1\n")]),
            "%B": iter([_capture("b-1\n"), _capture("b-2\n")]),
        }

        def fake_run(args, **_kw):
            target = args[args.index("-t") + 1]
            return next(captures[target])

        with patch("agent_crew.cli.subprocess.run", side_effect=fake_run):
            assert _pane_changed("%A") is False  # priming A
            assert _pane_changed("%B") is False  # priming B
            assert _pane_changed("%A") is False  # A unchanged
            assert _pane_changed("%B") is True   # B changed b-1 → b-2

    def test_subprocess_failure_returns_false(self):
        """Inconclusive readings should never auto-fail. Treat as idle
        and defer the decision to the next healthy tick."""
        cap = _capture("", returncode=1)
        with patch("agent_crew.cli.subprocess.run", return_value=cap):
            assert _pane_changed("%999") is False

    def test_reset_cache_makes_next_call_act_like_first(self):
        cap = _capture("anything\n")
        with patch("agent_crew.cli.subprocess.run", return_value=cap):
            assert _pane_changed("%999") is False  # priming
            assert _pane_changed("%999") is False  # idle (unchanged)
            _reset_pane_content_cache()
            # Baseline gone again; first-call semantics return False.
            assert _pane_changed("%999") is False
