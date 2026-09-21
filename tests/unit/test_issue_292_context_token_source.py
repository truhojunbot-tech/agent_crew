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
        assert sv._pane_token_count("%91") is None


def test_a_present_hint_is_still_parsed():
    with patch.object(sv.subprocess, "run", _pane(HINT)):
        assert sv._pane_token_count("%91") == 544_100


def test_a_pane_that_cannot_be_read_is_unknown():
    def boom(*a, **k):
        raise OSError("no tmux")
    with patch.object(sv.subprocess, "run", boom):
        assert sv._pane_token_count("%91") is None


# ── 2. the transcript is the ground truth ─────────────────────────────


def test_the_transcript_is_preferred_over_the_pane(tmp_path):
    """★★A pane showing no hint while the session holds 900k is the reported
    situation exactly; the transcript has to win."""
    _session(tmp_path, "/w/claude", {"cache_read_input_tokens": 900_000})
    with patch.object(sv.subprocess, "run", _pane(NO_HINT)):
        tokens, source = sv._context_token_count("%91", "/w/claude", agent="claude", home=tmp_path)
    assert tokens == 900_000 and source == "transcript"


def test_the_transcript_wins_even_when_the_hint_is_present(tmp_path):
    """⛔The hint is a rounded UI string; the transcript is the number that gets
    re-billed. When both exist they are not interchangeable, and the exact one
    is the one to threshold against."""
    _session(tmp_path, "/w/claude", {"cache_read_input_tokens": 123_456})
    with patch.object(sv.subprocess, "run", _pane(HINT)):
        tokens, source = sv._context_token_count("%91", "/w/claude", agent="claude", home=tmp_path)
    assert tokens == 123_456 and source == "transcript"


def test_a_measured_zero_from_the_transcript_is_not_unknown(tmp_path):
    """⛔#288's contract, carried through: a genuinely empty window is a
    measurement. It must not fall through to the pane hint as if nothing had
    been read."""
    _session(tmp_path, "/w/claude", {"cache_read_input_tokens": 0,
                                     "cache_creation_input_tokens": 0,
                                     "input_tokens": 0})
    with patch.object(sv.subprocess, "run", _pane(HINT)):
        tokens, source = sv._context_token_count("%91", "/w/claude", agent="claude", home=tmp_path)
    assert tokens == 0 and source == "transcript"


def test_without_a_transcript_the_pane_hint_is_the_fallback(tmp_path):
    """Providers other than Claude have no transcript to read; the old signal
    is still better than nothing for them."""
    with patch.object(sv.subprocess, "run", _pane(HINT)):
        tokens, source = sv._context_token_count("%91", "", agent="claude", home=tmp_path)
    assert tokens == 544_100 and source == "pane_hint"


def test_neither_source_is_unknown(tmp_path):
    with patch.object(sv.subprocess, "run", _pane(NO_HINT)):
        tokens, source = sv._context_token_count("%91", "/w/nothing", agent="claude", home=tmp_path)
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
    monkeypatch.setattr(sv, "_pane_clear_context", lambda pane, **_kw: cleared.append(pane))
    monkeypatch.setattr(sv, "_pane_alive_for_push", lambda pane: True)
    monkeypatch.setattr(sv, "_pane_has_usage_limit", lambda pane: False)
    monkeypatch.setattr(sv, "_pane_dismiss_permission_prompt", lambda pane: None)
    monkeypatch.setattr(sv.subprocess, "run", _pane(NO_HINT))
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")

    db = str(tmp_path / "tasks.db")
    pushed = []
    app = create_app(db_path=db, state_path=str(state),
                     pane_map={"implementer": "%91"}, port=0,
                     push_fn=lambda pane, text: pushed.append(pane),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        # Posted through the real endpoint: enqueueing is what triggers the
        # push path, so this exercises the trigger rather than a helper.
        client.post("/tasks", json={
            "task_id": "t-292", "task_type": "implement", "description": "go",
            "branch": "main", "priority": 3, "context": {}, "project": "demo"})

    assert cleared == ["%91"], "a saturated pane was pushed to without clearing"
    assert pushed == ["%91"]


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
    monkeypatch.setattr(sv, "_pane_clear_context", lambda pane, **_kw: cleared.append(pane))
    monkeypatch.setattr(sv, "_pane_alive_for_push", lambda pane: True)
    monkeypatch.setattr(sv, "_pane_has_usage_limit", lambda pane: False)
    monkeypatch.setattr(sv, "_pane_dismiss_permission_prompt", lambda pane: None)
    monkeypatch.setattr(sv.subprocess, "run", _pane(NO_HINT))
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")

    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, state_path=str(state),
                     pane_map={"implementer": "%91"}, port=0,
                     push_fn=lambda pane, text: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        client.post("/tasks", json={
            "task_id": "t-292b", "task_type": "implement", "description": "go",
            "branch": "main", "priority": 3, "context": {}, "project": "demo"})

    assert cleared == []


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


def test_a_non_claude_agent_never_reads_a_claude_transcript(tmp_path):
    """★★A codex pane must not be sized by a Claude transcript that happens to
    exist in the worktree it was handed."""
    _session(tmp_path, "/w/codex", {"cache_read_input_tokens": 900_000})
    with patch.object(sv.subprocess, "run", _pane(NO_HINT)):
        tokens, source = sv._context_token_count(
            "%92", "/w/codex", agent="codex", home=tmp_path)
    assert source != "transcript"
    assert tokens is None


def test_a_non_claude_agent_still_gets_the_pane_hint(tmp_path):
    """⛔The fallback is what those providers have; gating the transcript must
    not take the old signal away from them."""
    _session(tmp_path, "/w/codex", {"cache_read_input_tokens": 900_000})
    with patch.object(sv.subprocess, "run", _pane(HINT)):
        tokens, source = sv._context_token_count(
            "%92", "/w/codex", agent="codex", home=tmp_path)
    assert (tokens, source) == (544_100, "pane_hint")


def test_claude_still_reads_its_transcript(tmp_path):
    _session(tmp_path, "/w/claude", {"cache_read_input_tokens": 900_000})
    with patch.object(sv.subprocess, "run", _pane(NO_HINT)):
        tokens, source = sv._context_token_count(
            "%91", "/w/claude", agent="claude", home=tmp_path)
    assert (tokens, source) == (900_000, "transcript")


def test_an_unknown_agent_does_not_read_a_transcript(tmp_path):
    """⛔Fail closed on identity: if we cannot say the pane is Claude's, we
    cannot say the transcript is the one it is carrying."""
    _session(tmp_path, "/w/x", {"cache_read_input_tokens": 900_000})
    with patch.object(sv.subprocess, "run", _pane(NO_HINT)):
        assert sv._context_token_count("%99", "/w/x", agent="", home=tmp_path) == (None, "unknown")


def test_the_worktree_follows_the_agent_not_the_role():
    """The map is keyed by role in one mode and by agent in the other; either
    way the answer must be the worktree that AGENT works in."""
    by_agent = {"claude": "/w/claude", "codex": "/w/codex"}
    by_role = {"implementer": "/w/claude", "reviewer": "/w/codex"}
    assert sv._agent_worktree(by_agent, "codex", "implementer") == "/w/codex"
    assert sv._agent_worktree(by_role, "codex", "implementer") == "/w/codex"
    assert sv._agent_worktree(by_role, "claude", "reviewer") == "/w/claude"


def test_an_unlocatable_agent_worktree_is_empty_not_someone_elses():
    """⛔Returning the task's own role worktree as a fallback would reintroduce
    exactly the bug: measuring a worktree the target agent does not own."""
    assert sv._agent_worktree({"implementer": "/w/claude"}, "gemini", "implementer") == ""


def _override_push(tmp_path, monkeypatch, *, override, role_worktrees, claude_tokens):
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
    monkeypatch.setattr(sv, "_pane_has_usage_limit", lambda pane: False)
    monkeypatch.setattr(sv, "_pane_dismiss_permission_prompt", lambda pane: None)
    monkeypatch.setattr(sv.subprocess, "run", _pane(NO_HINT))
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")

    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, state_path=str(state),
                     pane_map={"implementer": "%91", "reviewer": "%92",
                               "claude": "%91", "codex": "%92"},
                     port=0, push_fn=lambda pane, text: pushed.append(pane),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        client.post("/tasks", json={
            "task_id": "t-override", "task_type": "implement", "description": "go",
            "branch": "main", "priority": 3, "project": "demo",
            "context": {"agent_override": override}})
    return cleared, pushed


def test_an_override_does_not_clear_the_wrong_pane(tmp_path, monkeypatch):
    """★★The reported failure. The implementer's Claude transcript is far over
    the threshold, but the task is routed to the codex pane — which must not be
    cleared on the strength of a window it is not carrying."""
    cleared, pushed = _override_push(
        tmp_path, monkeypatch, override="codex",
        role_worktrees={"implementer": "claude", "reviewer": "codex"},
        claude_tokens=900_000)
    assert pushed == ["%92"], pushed
    assert cleared == [], f"cleared the wrong pane: {cleared}"


def test_without_an_override_the_saturated_pane_is_still_cleared(tmp_path, monkeypatch):
    """⛔The control. Binding to the agent must not switch the guard off for the
    ordinary case #292 exists to fix."""
    cleared, pushed = _override_push(
        tmp_path, monkeypatch, override="claude",
        role_worktrees={"implementer": "claude", "reviewer": "codex"},
        claude_tokens=900_000)
    assert pushed == ["%91"]
    assert cleared == ["%91"]


def test_an_override_to_claude_reads_claudes_worktree_not_the_roles(tmp_path,
                                                                    monkeypatch):
    """★★The review's other scenario, and the one the agent gate alone does NOT
    cover: a reviewer task overridden to claude uses the claude pane, so the
    gate passes — but if the worktree is still keyed on the task's role it
    points at codex's directory, and a stale Claude transcript sitting there is
    measured instead of the real one.

    Found by mutation: reverting the worktree lookup to `worktree_map[role]`
    killed no test until this one existed."""
    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    claude_wt = tmp_path / "worktrees" / "claude"
    codex_wt = tmp_path / "worktrees" / "codex"
    for d in (claude_wt, codex_wt):
        d.mkdir(parents=True)
    # Claude's own session is small; a STALE one sits in codex's worktree.
    _session(tmp_path / "claudehome", str(claude_wt), {"cache_read_input_tokens": 10})
    _session(tmp_path / "claudehome", str(codex_wt), {"cache_read_input_tokens": 900_000})

    state = tmp_path / "state.json"
    state.write_text(json.dumps({"port": 0, "worktrees": {
        "claude": str(claude_wt), "codex": str(codex_wt)}}))

    cleared, pushed = [], []
    monkeypatch.setattr(sv, "_claude_home", lambda home=None: tmp_path / "claudehome")
    monkeypatch.setattr(sv, "_pane_clear_context", lambda pane, **_kw: cleared.append(pane))
    monkeypatch.setattr(sv, "_pane_alive_for_push", lambda pane: True)
    monkeypatch.setattr(sv, "_pane_has_usage_limit", lambda pane: False)
    monkeypatch.setattr(sv, "_pane_dismiss_permission_prompt", lambda pane: None)
    monkeypatch.setattr(sv.subprocess, "run", _pane(NO_HINT))
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")

    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, state_path=str(state),
                     pane_map={"implementer": "%91", "reviewer": "%92",
                               "claude": "%91", "codex": "%92"},
                     port=0, push_fn=lambda pane, text: pushed.append(pane),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        client.post("/tasks", json={
            "task_id": "t-rev-override", "task_type": "review",
            "description": "review", "branch": "main", "priority": 3,
            "project": "demo", "context": {"agent_override": "claude"}})

    assert pushed == ["%91"], pushed
    assert cleared == [], "cleared on a transcript from a worktree claude does not own"


def test_the_discuss_path_gates_on_its_agent_too(tmp_path, monkeypatch):
    """⛔The discuss path takes the agent directly, so it looked safe — but
    nothing asserted the gate until mutation forced `agent="claude"` there and
    killed no test. A codex panel must not be sized by a Claude transcript."""
    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    codex_wt = tmp_path / "worktrees" / "codex"
    codex_wt.mkdir(parents=True)
    _session(tmp_path / "claudehome", str(codex_wt), {"cache_read_input_tokens": 900_000})

    state = tmp_path / "state.json"
    state.write_text(json.dumps({"port": 0, "worktrees": {"codex": str(codex_wt)}}))

    cleared, pushed = [], []
    monkeypatch.setattr(sv, "_claude_home", lambda home=None: tmp_path / "claudehome")
    monkeypatch.setattr(sv, "_pane_clear_context", lambda pane, **_kw: cleared.append(pane))
    monkeypatch.setattr(sv, "_pane_alive_for_push", lambda pane: True)
    monkeypatch.setattr(sv, "_pane_dismiss_permission_prompt", lambda pane: None)
    monkeypatch.setattr(sv.subprocess, "run", _pane(NO_HINT))
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")

    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, state_path=str(state),
                     pane_map={"codex": "%92", "reviewer": "%92"}, port=0,
                     push_fn=lambda pane, text: pushed.append(pane),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        client.post("/tasks", json={
            "task_id": "t-discuss", "task_type": "discuss",
            "description": "discuss", "branch": "main", "priority": 3,
            "project": "demo", "context": {"agent": "codex"}})

    assert pushed == ["%92"], pushed
    assert cleared == [], "a codex panel was cleared on a Claude transcript"


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
    assert sv._agent_worktree(ROLE_MAP, "claude", "") == "/w/impl"


def test_an_agent_in_no_role_resolves_to_nothing():
    assert sv._agent_worktree(ROLE_MAP, "nobody", "reviewer", role_to_agent=LIVE) == ""


def test_a_configured_claude_reviewer_is_not_sized_by_the_implementer(tmp_path,
                                                                      monkeypatch):
    """★★End to end: claude on BOTH roles, a huge implementer transcript and a
    small reviewer one. Pushing the reviewer task must read the reviewer's."""
    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    impl_wt = tmp_path / "worktrees" / "claude-impl"
    rev_wt = tmp_path / "worktrees" / "claude-rev"
    for d in (impl_wt, rev_wt):
        d.mkdir(parents=True)
    _session(tmp_path / "claudehome", str(impl_wt), {"cache_read_input_tokens": 900_000})
    _session(tmp_path / "claudehome", str(rev_wt), {"cache_read_input_tokens": 10})

    state = tmp_path / "state.json"
    state.write_text(json.dumps({"port": 0, "roles": [
        {"role": "implementer", "agent": "claude", "worktree": str(impl_wt)},
        {"role": "reviewer", "agent": "claude", "worktree": str(rev_wt)},
    ]}))

    cleared, pushed = [], []
    monkeypatch.setattr(sv, "_claude_home", lambda home=None: tmp_path / "claudehome")
    monkeypatch.setattr(sv, "_pane_clear_context", lambda pane, **_kw: cleared.append(pane))
    monkeypatch.setattr(sv, "_pane_alive_for_push", lambda pane: True)
    monkeypatch.setattr(sv, "_pane_has_usage_limit", lambda pane: False)
    monkeypatch.setattr(sv, "_pane_dismiss_permission_prompt", lambda pane: None)
    monkeypatch.setattr(sv.subprocess, "run", _pane(NO_HINT))
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")

    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, state_path=str(state),
                     pane_map={"implementer": "%91", "reviewer": "%92"}, port=0,
                     push_fn=lambda pane, text: pushed.append(pane),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        client.post("/tasks", json={
            "task_id": "t-rev-claude", "task_type": "review",
            "description": "review", "branch": "main", "priority": 3,
            "project": "demo", "context": {}})

    assert pushed == ["%92"], pushed
    assert cleared == [], "the reviewer pane was cleared on the implementer's window"


def test_the_configured_implementer_is_still_cleared_when_saturated(tmp_path,
                                                                    monkeypatch):
    """⛔The control: reading the right worktree must still fire when THAT one
    is over the threshold."""
    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    impl_wt = tmp_path / "worktrees" / "claude-impl"
    rev_wt = tmp_path / "worktrees" / "claude-rev"
    for d in (impl_wt, rev_wt):
        d.mkdir(parents=True)
    _session(tmp_path / "claudehome", str(impl_wt), {"cache_read_input_tokens": 900_000})
    _session(tmp_path / "claudehome", str(rev_wt), {"cache_read_input_tokens": 10})

    state = tmp_path / "state.json"
    state.write_text(json.dumps({"port": 0, "roles": [
        {"role": "implementer", "agent": "claude", "worktree": str(impl_wt)},
        {"role": "reviewer", "agent": "claude", "worktree": str(rev_wt)},
    ]}))

    cleared, pushed = [], []
    monkeypatch.setattr(sv, "_claude_home", lambda home=None: tmp_path / "claudehome")
    monkeypatch.setattr(sv, "_pane_clear_context", lambda pane, **_kw: cleared.append(pane))
    monkeypatch.setattr(sv, "_pane_alive_for_push", lambda pane: True)
    monkeypatch.setattr(sv, "_pane_has_usage_limit", lambda pane: False)
    monkeypatch.setattr(sv, "_pane_dismiss_permission_prompt", lambda pane: None)
    monkeypatch.setattr(sv.subprocess, "run", _pane(NO_HINT))
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")

    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, state_path=str(state),
                     pane_map={"implementer": "%91", "reviewer": "%92"}, port=0,
                     push_fn=lambda pane, text: pushed.append(pane),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        client.post("/tasks", json={
            "task_id": "t-impl-claude", "task_type": "implement",
            "description": "go", "branch": "main", "priority": 3,
            "project": "demo", "context": {}})

    assert pushed == ["%91"]
    assert cleared == ["%91"]


def test_the_discuss_path_uses_the_live_role_map_too(tmp_path, monkeypatch):
    """⛔The discuss call site also has to pass the live mapping, and nothing
    asserted it: every earlier discuss test used an agent-keyed worktree map,
    where the role mapping is never consulted. Mutation caught that.

    Claude holds two roles here, so a discuss task for claude cannot say which
    worktree its pane is in — the honest answer is unknown, and unknown clears
    nothing. Under the static default it would have resolved to the implementer
    worktree and `/clear`ed the panel on a window it is not carrying."""
    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    impl_wt = tmp_path / "worktrees" / "claude-impl"
    rev_wt = tmp_path / "worktrees" / "claude-rev"
    for d in (impl_wt, rev_wt):
        d.mkdir(parents=True)
    _session(tmp_path / "claudehome", str(impl_wt), {"cache_read_input_tokens": 900_000})
    _session(tmp_path / "claudehome", str(rev_wt), {"cache_read_input_tokens": 10})

    state = tmp_path / "state.json"
    state.write_text(json.dumps({"port": 0, "roles": [
        {"role": "implementer", "agent": "claude", "worktree": str(impl_wt)},
        {"role": "reviewer", "agent": "claude", "worktree": str(rev_wt)},
    ]}))

    cleared, pushed = [], []
    monkeypatch.setattr(sv, "_claude_home", lambda home=None: tmp_path / "claudehome")
    monkeypatch.setattr(sv, "_pane_clear_context", lambda pane, **_kw: cleared.append(pane))
    monkeypatch.setattr(sv, "_pane_alive_for_push", lambda pane: True)
    monkeypatch.setattr(sv, "_pane_dismiss_permission_prompt", lambda pane: None)
    monkeypatch.setattr(sv.subprocess, "run", _pane(NO_HINT))
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")

    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, state_path=str(state),
                     pane_map={"claude": "%91", "implementer": "%91"}, port=0,
                     push_fn=lambda pane, text: pushed.append(pane),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        client.post("/tasks", json={
            "task_id": "t-discuss-live", "task_type": "discuss",
            "description": "discuss", "branch": "main", "priority": 3,
            "project": "demo", "context": {"agent": "claude"}})

    assert pushed == ["%91"], pushed
    assert cleared == [], "cleared on a worktree the panel's role could not be tied to"
