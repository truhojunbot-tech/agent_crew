"""Regression tests for issue #195 — crew recover agent-liveness check.

Before this fix, `crew recover` only verified server-up + tmux-session-up
and replied "Nothing to recover". A crashed codex/gemini agent that left
its pane sitting at a bash prompt slipped through, and review tasks
silently failed against stale code.

`_detect_dead_agent_panes` now flags panes whose foreground command is a
plain shell (bash/sh/zsh/...). Recover surfaces them in its output so the
operator sees what needs attention.
"""
from unittest.mock import patch

from agent_crew.cli import _detect_dead_agent_panes


def _fake_cmd_lookup(mapping: dict):
    """Patch helper: pane_id → current_command."""
    def fake_alive(pane_id):
        return pane_id in mapping
    def fake_cmd(pane_id):
        return mapping.get(pane_id, "")
    return fake_alive, fake_cmd


def test_b195_dispatcher_mode_python3_panes_are_healthy():
    alive, cmd = _fake_cmd_lookup({"%1": "python3", "%2": "python3", "%3": "python3"})
    with patch("agent_crew.cli._pane_alive", side_effect=alive), \
         patch("agent_crew.cli._pane_current_command", side_effect=cmd):
        dead = _detect_dead_agent_panes(
            ["claude", "codex", "gemini"], ["%1", "%2", "%3"], dispatcher_mode=True,
        )
    assert dead == []


def test_b195_dispatcher_mode_bash_pane_flagged():
    """Log viewer crashed → pane sitting at bash prompt is the #195 signature."""
    alive, cmd = _fake_cmd_lookup({"%1": "python3", "%2": "bash", "%3": "python3"})
    with patch("agent_crew.cli._pane_alive", side_effect=alive), \
         patch("agent_crew.cli._pane_current_command", side_effect=cmd):
        dead = _detect_dead_agent_panes(
            ["claude", "codex", "gemini"], ["%1", "%2", "%3"], dispatcher_mode=True,
        )
    assert dead == [("codex", "%2", "bash")]


def test_b195_legacy_mode_agent_cli_panes_are_healthy():
    alive, cmd = _fake_cmd_lookup({"%1": "claude", "%2": "codex", "%3": "node"})
    with patch("agent_crew.cli._pane_alive", side_effect=alive), \
         patch("agent_crew.cli._pane_current_command", side_effect=cmd):
        dead = _detect_dead_agent_panes(
            ["claude", "codex", "gemini"], ["%1", "%2", "%3"], dispatcher_mode=False,
        )
    assert dead == []


def test_b195_legacy_mode_codex_crashed_to_bash():
    alive, cmd = _fake_cmd_lookup({"%1": "claude", "%2": "bash", "%3": "node"})
    with patch("agent_crew.cli._pane_alive", side_effect=alive), \
         patch("agent_crew.cli._pane_current_command", side_effect=cmd):
        dead = _detect_dead_agent_panes(
            ["claude", "codex", "gemini"], ["%1", "%2", "%3"], dispatcher_mode=False,
        )
    assert dead == [("codex", "%2", "bash")]


def test_b195_missing_pane_flagged():
    """Pane id present in state but gone from tmux → flag as crashed."""
    alive, cmd = _fake_cmd_lookup({"%1": "python3", "%3": "python3"})  # %2 absent
    with patch("agent_crew.cli._pane_alive", side_effect=alive), \
         patch("agent_crew.cli._pane_current_command", side_effect=cmd):
        dead = _detect_dead_agent_panes(
            ["claude", "codex", "gemini"], ["%1", "%2", "%3"], dispatcher_mode=True,
        )
    assert ("codex", "%2", "(missing)") in dead
    assert len(dead) == 1


def test_b195_all_shells_count_as_dead():
    """Any standard shell prompt counts as a crash signature."""
    for sh in ("bash", "sh", "zsh", "fish", "dash"):
        alive, cmd = _fake_cmd_lookup({"%1": sh})
        with patch("agent_crew.cli._pane_alive", side_effect=alive), \
             patch("agent_crew.cli._pane_current_command", side_effect=cmd):
            dead = _detect_dead_agent_panes(
                ["claude"], ["%1"], dispatcher_mode=True,
            )
        assert dead == [("claude", "%1", sh)], f"shell {sh!r} not flagged"
