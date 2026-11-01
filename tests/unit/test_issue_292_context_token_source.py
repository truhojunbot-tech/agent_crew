"""#292 — the auto-clear was measuring a hint that is usually not on screen.

`_pane_token_count` scraped `tmux capture-pane` for Claude Code's
"/clear to save Xk tokens" footer. No `-S`, so only the visible screen; and the
hint is only rendered in some UI states. When it was absent the regex found
nothing and the function returned **0**, which reads as "well under threshold"
rather than "unknown" — so the 200,000 auto-clear silently never fired.

Measured on this host, 2026-09-13, with the threshold at 200,000:

    project            pane   hint?   transcript tokens   verdict
    agent_council      %922    no                 62,025   under
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

pytestmark = pytest.mark.usefixtures("pinned_task_transport")

from agent_crew import server as sv


def _pane(text):
    class _R:
        returncode = 0
        stdout = text
        stderr = ""

    class _MissingAddendum:
        returncode = 128
        stdout = b""
        stderr = b""

    def run(argv, *args, **kwargs):
        # Protocol regeneration now probes the tracked base-ref addendum.
        # This pane fixture has no such file; do not return pane text as git output.
        if (isinstance(argv, (list, tuple)) and "show" in argv
                and any(isinstance(arg, str) and ":.agent_crew/" in arg for arg in argv)):
            return _MissingAddendum()
        return _R()

    return run


def _session(home, cwd, usage):
    d = home / "projects" / re.sub(r"[/._]", "-", str(cwd))
    d.mkdir(parents=True, exist_ok=True)
    (d / "s.jsonl").write_text(
        json.dumps({"type": "assistant", "message": {"usage": usage}}) + "\n")


HINT = "  new task? /clear to save 544.1k tokens\n"
NO_HINT = "  [Edit] /tmp/wt/tests/unit/test_thing.py\n  → 41L: # ...\n"


# ── 1. absent hint is unknown, not zero ───────────────────────────────








# ── 2. the transcript is the ground truth ─────────────────────────────












# ── 3. what the caller does with it ───────────────────────────────────


def _decide(tokens):
    """The clear decision, isolated from the push plumbing."""
    return sv._should_clear_context(tokens, threshold=200_000)












# ── 4. the real push paths ────────────────────────────────────────────






# ── 5. the measurement must follow the TARGET AGENT ───────────────────
#
# Review of PR #293, P1. `agent_override` repoints `pane_id` at another
# provider's pane, but the measurement still took `worktree_map[role]` and fed
# it to `claude_context_tokens()` unconditionally. Two ways that goes wrong:
#
#   * an implementer task overridden to codex uses the codex pane while reading
#     the implementer/Claude transcript — a big Claude window then sends
#     `/clear` to a codex pane that has nothing to do with it;
#   * a reviewer task overridden to claude uses the claude pane while pointing
#     at the reviewer/codex worktree — if a stale Claude transcript happens to
#     sit there, it is measured instead of the real one.
#
# ⛔The transcript reader is Claude-specific. Binding it to a path alone was the
#   defect; it has to be bound to the agent that path belongs to.














def _override_push(tmp_path, monkeypatch, *, override, role_worktrees, claude_tokens, unused_tcp_port):
    """Push one task with an agent_override; return the panes that got /clear."""
    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    # ⛔state.json's legacy `worktrees` map is keyed by AGENT; the server maps
    #   it to roles on load. Keying it by role here produced an empty
    #   worktree_map and the assertion failed for a fixture reason rather than
    #   a code one.
    worktrees = {}
    for agent_name in role_worktrees.values():
        wt = tmp_path / "worktrees" / agent_name
        wt.mkdir(parents=True, exist_ok=True)
        worktrees[agent_name] = str(wt)
    _session(tmp_path / "claudehome", worktrees["claude"],
             {"cache_read_input_tokens": claude_tokens})

    state = tmp_path / "state.json"
    state.write_text(json.dumps({"port": 0, "worktrees": worktrees}))

    cleared, pushed = [], []
    monkeypatch.setattr(sv, "_claude_home", lambda home=None: tmp_path / "claudehome")
    monkeypatch.setattr(sv, "_pane_clear_context", lambda pane, **_kw: cleared.append(pane))
    monkeypatch.setattr(sv, "_pane_alive_for_push", lambda pane: True)
    monkeypatch.setattr(sv, "_pane_dismiss_permission_prompt", lambda pane: None)
    monkeypatch.setattr(sv.subprocess, "run", _pane(NO_HINT))
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")

    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, state_path=str(state),
                     pane_map={"implementer": "%91", "reviewer": "%92",
                               "claude": "%91", "codex": "%92"},
                     port=unused_tcp_port, push_fn=lambda pane, text: pushed.append(pane),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        client.post("/tasks", json={
            "task_id": "t-override", "task_type": "implement", "description": "go",
            "branch": "main", "priority": 3, "project": "demo",
            "context": {"agent_override": override}})
    return cleared, pushed










# ── 6. the role map is configurable, so the lookup must read the live one ──
#
# Review of PR #293, round 2, P1. `_agent_worktree` resolved role-keyed maps
# through the STATIC `_DEFAULT_AGENT_TO_ROLE`, so `(map, "claude", "reviewer")`
# always returned the implementer worktree and its `role` argument did nothing.
# state.json explicitly supports custom assignments and the same provider on
# several roles, so a configured Claude reviewer read the IMPLEMENTER's
# transcript and could `/clear` the reviewer pane on it.


LIVE = {"implementer": "claude", "reviewer": "claude", "tester": "gemini"}
ROLE_MAP = {"implementer": "/w/impl", "reviewer": "/w/rev", "tester": "/w/test"}


def test_role_identity_is_preserved_when_one_agent_holds_two_roles():
    """★★The finding. Claude on both roles: a reviewer task must resolve the
    REVIEWER worktree, not whichever role the static default happens to name."""
    assert sv._agent_worktree(ROLE_MAP, "claude", "reviewer",
                              role_to_agent=LIVE) == "/w/rev"
    assert sv._agent_worktree(ROLE_MAP, "claude", "implementer",
                              role_to_agent=LIVE) == "/w/impl"


def test_a_custom_single_role_assignment_is_followed():
    """⛔The static map says codex reviews. A config that says otherwise has to
    win, or the lookup is describing a deployment that does not exist."""
    live = {"implementer": "codex", "reviewer": "gemini", "tester": "claude"}
    assert sv._agent_worktree(ROLE_MAP, "claude", "", role_to_agent=live) == "/w/test"
    assert sv._agent_worktree(ROLE_MAP, "codex", "", role_to_agent=live) == "/w/impl"


def test_an_override_to_an_agent_with_one_role_still_resolves():
    """A reviewer task overridden to claude, where claude only implements: the
    target role is not claude's, but claude has exactly one worktree and that
    is unambiguously the one its pane is in."""
    live = {"implementer": "claude", "reviewer": "codex", "tester": "gemini"}
    assert sv._agent_worktree(ROLE_MAP, "claude", "reviewer",
                              role_to_agent=live) == "/w/impl"


def test_an_ambiguous_override_resolves_to_nothing():
    """⛔Claude holds two roles and the task's role is neither. There is no way
    to say which worktree that pane is in, and guessing is how this whole class
    of bug happens — unknown is the honest answer and clears nothing."""
    assert sv._agent_worktree(ROLE_MAP, "claude", "tester", role_to_agent=LIVE) == ""


def test_an_agent_keyed_map_is_unaffected():
    """The other state.json spelling needs no role reasoning at all."""
    by_agent = {"claude": "/w/claude", "codex": "/w/codex"}
    assert sv._agent_worktree(by_agent, "claude", "reviewer",
                              role_to_agent=LIVE) == "/w/claude"


def test_the_static_default_is_still_the_fallback():
    """Legacy setups with no roles list keep working."""
    assert sv._agent_worktree(ROLE_MAP, "claude", "") == "/w/rev"


def test_an_agent_in_no_role_resolves_to_nothing():
    assert sv._agent_worktree(ROLE_MAP, "nobody", "reviewer", role_to_agent=LIVE) == ""
