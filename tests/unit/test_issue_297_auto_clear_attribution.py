"""#297 — an auto-clear that nothing records turns a resume into a lie.

#293 replaced the usually-invisible tmux footer hint with transcript-based
measurement, so the 200k auto-clear stopped being blind — five of seven
worktrees were over the threshold when it was measured. That makes `/clear`
materially more likely to fire, and `_pane_clear_context` emitted nothing at
all: no event, no identity, no mark on the task it preceded.

The contamination that allows:

    1. the context is classified `resume`
    2. the pane measures over threshold and `/clear` is sent before the push
    3. the provider runs with cleared conversational state
    4. the economics stay attached to a context whose policy still says `resume`

which is exactly the resume-vs-fresh benchmark the consumer is building.

⛔The treatment is corrected with the mechanism that already means this, not a
  new parallel concept: `task.context["context_reset"]` is what an operator
  sets to force a fresh context, and after a real `/clear` that is simply true.
  The next context resolution bumps the generation and records `fresh`, so a
  cohort keyed on the recorded policy cannot pick the task up as a resume.

⛔`send-keys` returning 0 proves the keystrokes were delivered, not that the
  provider cleared. Recorded as `attempted`, never `completed`.
"""

import json

import pytest
from fastapi.testclient import TestClient

from agent_crew import server as sv
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app

BIG = 900_000


def _pane(text=""):
    class _R:
        returncode = 0
        stdout = text
        stderr = ""
    return _R()


def _session(home, cwd, tokens):
    import re

    d = home / "projects" / re.sub(r"[/._]", "-", str(cwd))
    d.mkdir(parents=True, exist_ok=True)
    (d / "s.jsonl").write_text(json.dumps(
        {"type": "assistant",
         "message": {"usage": {"cache_read_input_tokens": tokens}}}) + "\n")


def _events(db_path):
    import os

    path = os.path.join(os.path.dirname(db_path), "context_events.jsonl")
    if not os.path.exists(path):
        return []
    return [json.loads(line) for line in open(path)]


def _cleared_events(db_path):
    return [e for e in _events(db_path)
            if e.get("event_type") == "provider_context_cleared"]


def _run(tmp_path, monkeypatch, *, tokens=BIG, task_type="implement",
         context=None, agent_key="claude", pane="%1"):
    """Push one task through the real path; return (db, sent_keys, task_ctx)."""
    wt = tmp_path / "worktrees" / "demo" / agent_key
    wt.mkdir(parents=True, exist_ok=True)
    _session(tmp_path / "claudehome", str(wt), tokens)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"port": 0, "worktrees": {agent_key: str(wt)}}))

    sent = []

    def fake_run(cmd, **kw):
        if isinstance(cmd, list) and "send-keys" in cmd:
            sent.append(cmd)
        return _pane()

    monkeypatch.setattr(sv, "_claude_home", lambda home=None: tmp_path / "claudehome")
    monkeypatch.setattr(sv, "_pane_alive_for_push", lambda p: True)
    monkeypatch.setattr(sv, "_pane_has_usage_limit", lambda p: False)
    monkeypatch.setattr(sv, "_pane_dismiss_permission_prompt", lambda p: None)
    monkeypatch.setattr(sv.subprocess, "run", fake_run)
    monkeypatch.setattr(sv.time, "sleep", lambda *_a: None)
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")

    db = str(tmp_path / "tasks.db")
    pane_map = {"implementer": pane, "claude": pane, "codex": pane,
                "reviewer": pane, "tester": pane}
    app = create_app(db_path=db, state_path=str(state), pane_map=pane_map,
                     port=0, push_fn=lambda p, t: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        client.post("/tasks", json={
            "task_id": "t-297", "task_type": task_type, "description": "go",
            "branch": "main", "priority": 3, "project": "demo",
            "context": context or {}})
        ctx = TaskQueue(db).get_task_context("t-297")
    return db, sent, ctx


# ── 1. the clear leaves a durable record ──────────────────────────────


def test_an_auto_clear_emits_a_lifecycle_event(tmp_path, monkeypatch):
    """★★The bug: `_pane_clear_context` recorded nothing at all."""
    db, sent, _ = _run(tmp_path, monkeypatch)
    assert any("/clear" in c for c in sent), "no /clear was sent"
    assert len(_cleared_events(db)) == 1


def test_the_event_carries_what_an_audit_needs(tmp_path, monkeypatch):
    db, _, _ = _run(tmp_path, monkeypatch)
    event = _cleared_events(db)[0]
    assert event["task_id"] == "t-297"
    assert event["agent"] == "claude"
    assert event["pane_id"] == "%1"
    assert event["reason"] == "auto_clear_token_threshold"


def test_the_event_carries_the_measurement_that_triggered_it(tmp_path, monkeypatch):
    """⛔The number and where it came from. A threshold event without the
    reading that crossed it cannot be audited or replayed."""
    db, _, _ = _run(tmp_path, monkeypatch)
    event = _cleared_events(db)[0]
    assert event["context_tokens"] == BIG
    assert event["token_source"] == "transcript"
    assert event["cap_tokens"] == sv._TOKEN_CLEAR_THRESHOLD


def test_the_clear_is_recorded_as_attempted_not_completed(tmp_path, monkeypatch):
    """⛔`send-keys` returning 0 proves the keystrokes were delivered, not that
    the provider cleared anything. Claiming `completed` would be inventing a
    confirmation the transport cannot give."""
    db, _, _ = _run(tmp_path, monkeypatch)
    assert _cleared_events(db)[0]["outcome"] == "attempted"


def test_a_failed_send_is_recorded_as_such(tmp_path, monkeypatch):
    """The one thing send-keys CAN tell us."""
    calls = []

    def failing(cmd, **kw):
        if isinstance(cmd, list) and "send-keys" in cmd:
            calls.append(cmd)

            class _R:
                returncode = 1
                stdout = ""
                stderr = "no such pane"
            return _R()
        return _pane()

    monkeypatch.setattr(sv.time, "sleep", lambda *_a: None)
    with monkeypatch.context() as m:
        m.setattr(sv.subprocess, "run", failing)
        sv._pane_clear_context("%9", events_path=str(tmp_path / "e.jsonl"),
                               task_id="t", agent="claude")
    lines = [json.loads(x) for x in open(tmp_path / "e.jsonl")]
    assert lines[0]["outcome"] == "send_failed"


def test_no_clear_means_no_event(tmp_path, monkeypatch):
    """⛔The control. An event stream that records non-events is worse than
    none — every rate computed from it would be wrong."""
    db, sent, _ = _run(tmp_path, monkeypatch, tokens=10)
    assert not any("/clear" in c for c in sent)
    assert _cleared_events(db) == []


# ── 2. the treatment is corrected, not just described ─────────────────


def test_the_cleared_task_is_marked_for_a_fresh_context(tmp_path, monkeypatch):
    """★★The integration bug. `context_reset` is what an operator sets to force
    a fresh context, and after a real `/clear` it is simply true — so the next
    resolution bumps the generation and records `fresh` instead of `resume`."""
    _, _, ctx = _run(tmp_path, monkeypatch)
    assert ctx["context_reset"] is True


def test_a_task_that_was_already_a_resume_is_still_corrected(tmp_path, monkeypatch):
    """★★Acceptance 4's case: a pre-existing resume whose pane is over
    threshold. This is precisely the row that would otherwise enter a
    resume cohort having actually run fresh."""
    _, _, ctx = _run(tmp_path, monkeypatch, context={"context_policy": "resume"})
    assert ctx["context_reset"] is True


def test_the_intervention_is_joinable_on_its_own(tmp_path, monkeypatch):
    """⛔Acceptance 2's other half: a consumer must be able to EXCLUDE or
    stratify auto-cleared rows specifically, not merely see `fresh` and wonder
    which of several reasons produced it."""
    _, _, ctx = _run(tmp_path, monkeypatch)
    assert ctx["auto_cleared_before_push"] is True
    assert ctx["auto_clear_context_tokens"] == BIG
    assert ctx["auto_clear_token_source"] == "transcript"


def test_an_uncleared_task_carries_no_intervention_marks(tmp_path, monkeypatch):
    _, _, ctx = _run(tmp_path, monkeypatch, tokens=10)
    for key in ("context_reset", "auto_cleared_before_push"):
        assert key not in ctx, key


def test_an_explicit_operator_reset_is_not_overwritten(tmp_path, monkeypatch):
    """⛔The marker is additive. An operator who already asked for a reset must
    not have their intent relabelled as an auto-clear."""
    _, _, ctx = _run(tmp_path, monkeypatch, tokens=10,
                     context={"context_reset": True})
    assert ctx["context_reset"] is True
    assert "auto_cleared_before_push" not in ctx


# ── 3. both push paths ────────────────────────────────────────────────


def test_the_discuss_path_is_attributed_too(tmp_path, monkeypatch):
    """⛔Acceptance 4. Panels accumulate the most context, which is why #260 put
    a guard on this path — so it is the path most likely to clear."""
    db, sent, _ = _run(tmp_path, monkeypatch, task_type="discuss",
                       context={"agent": "claude"})
    assert any("/clear" in c for c in sent)
    events = _cleared_events(db)
    assert len(events) == 1 and events[0]["task_id"] == "t-297"


def test_the_discuss_task_is_also_marked(tmp_path, monkeypatch):
    _, _, ctx = _run(tmp_path, monkeypatch, task_type="discuss",
                     context={"agent": "claude"})
    assert ctx["context_reset"] is True and ctx["auto_cleared_before_push"] is True


# ── 4. identity, where the push path can know it ──────────────────────


def test_a_known_context_identity_is_attached(tmp_path, monkeypatch):
    """The push path does not resolve context identity itself — that happens at
    dispatch — so the event reads the recorded one rather than minting it.
    `task_id` is the join key either way; this is the audit detail."""
    db = str(tmp_path / "peek.db")
    q = TaskQueue(db)
    q.get_or_create_context(project="demo", agent="claude",
                            worktree_path="/w/claude", task_id="seed")
    identity = q.peek_context_identity("demo", "claude", "/w/claude")
    assert identity["context_id"]
    assert identity["context_generation"] >= 1


def test_an_unknown_context_identity_is_absent_not_invented(tmp_path):
    q = TaskQueue(str(tmp_path / "empty.db"))
    assert q.peek_context_identity("nobody", "nothing", "/nowhere") == {}


# ── 5. a clear that did not happen must not be recorded as one ────────
#
# Review of PR #299. Two findings:
#
#   P1: `_pane_clear_context` returns False when `send-keys` fails and records
#       `outcome=send_failed` — and both callers discarded that result and
#       marked the task anyway. `context_reset: true` for a pane that still
#       holds its whole conversation reverses #297's own guarantee: instead of
#       a cleared task hiding in the `resume` cohort, an UNcleared one lands in
#       `fresh`. Same contamination, opposite direction.
#   P2: the discuss event derived `role` from the static default rather than
#       the live map, so a configured custom role was misattributed — the same
#       class of bug as #292's round-2 finding, in the event's role field.


def _failing_send(tmp_path, monkeypatch, *, task_type="implement", context=None):
    """Same push, but tmux refuses the keystrokes."""
    wt = tmp_path / "worktrees" / "demo" / "claude"
    wt.mkdir(parents=True, exist_ok=True)
    _session(tmp_path / "claudehome", str(wt), BIG)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"port": 0, "worktrees": {"claude": str(wt)}}))

    def fake_run(cmd, **kw):
        if isinstance(cmd, list) and "send-keys" in cmd and "/clear" in cmd:
            class _R:
                returncode = 1
                stdout = ""
                stderr = "can't find pane"
            return _R()
        return _pane()

    monkeypatch.setattr(sv, "_claude_home", lambda home=None: tmp_path / "claudehome")
    monkeypatch.setattr(sv, "_pane_alive_for_push", lambda p: True)
    monkeypatch.setattr(sv, "_pane_has_usage_limit", lambda p: False)
    monkeypatch.setattr(sv, "_pane_dismiss_permission_prompt", lambda p: None)
    monkeypatch.setattr(sv.subprocess, "run", fake_run)
    monkeypatch.setattr(sv.time, "sleep", lambda *_a: None)
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")

    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, state_path=str(state),
                     pane_map={"implementer": "%1", "claude": "%1"}, port=0,
                     push_fn=lambda p, t: None, watchdog_disabled=True,
                     anomaly_disabled=True)
    with TestClient(app) as client:
        client.post("/tasks", json={
            "task_id": "t-299", "task_type": task_type, "description": "go",
            "branch": "main", "priority": 3, "project": "demo",
            "context": context or {}})
        ctx = TaskQueue(db).get_task_context("t-299")
    return db, ctx


def test_a_failed_send_does_not_claim_a_fresh_context(tmp_path, monkeypatch):
    """★★The finding. Marking `context_reset` for a pane that was not cleared
    puts an UNcleared task into the `fresh` cohort — the same contamination
    #297 exists to prevent, pointing the other way."""
    _, ctx = _failing_send(tmp_path, monkeypatch)
    assert "context_reset" not in ctx, ctx
    assert "auto_cleared_before_push" not in ctx, ctx


def test_a_failed_send_on_the_discuss_path_is_also_unmarked(tmp_path, monkeypatch):
    _, ctx = _failing_send(tmp_path, monkeypatch, task_type="discuss",
                           context={"agent": "claude"})
    assert "context_reset" not in ctx
    assert "auto_cleared_before_push" not in ctx


def test_a_failed_send_is_still_recorded(tmp_path, monkeypatch):
    """⛔The attempt is still a fact, and the one the operator most needs: a
    pane over threshold that could not be cleared is unguarded. Not marking the
    task must not mean staying silent about it."""
    db, _ = _failing_send(tmp_path, monkeypatch)
    events = _cleared_events(db)
    assert len(events) == 1
    assert events[0]["outcome"] == "send_failed"


def test_a_successful_send_is_still_marked(tmp_path, monkeypatch):
    """⛔The control, so the fix is not simply "never mark"."""
    _, _, ctx = _run(tmp_path, monkeypatch)
    assert ctx["context_reset"] is True and ctx["auto_cleared_before_push"] is True


# ── 6. the event's role comes from the live map ───────────────────────


def test_the_discuss_event_uses_the_configured_role(tmp_path, monkeypatch):
    """★★P2. The static default says claude implements; a config that says
    otherwise has to win, or the event describes a deployment that does not
    exist."""
    wt = tmp_path / "worktrees" / "claude"
    wt.mkdir(parents=True)
    _session(tmp_path / "claudehome", str(wt), BIG)
    state = tmp_path / "state.json"
    # ⛔All three roles listed. `_load_role_to_agent` starts from the defaults
    #   and only overrides what is present, so naming reviewer alone would leave
    #   claude holding implementer TOO — ambiguous, and the lookup would
    #   correctly refuse to name a role. The config has to actually make claude
    #   single-roled for this test to be about the live map.
    state.write_text(json.dumps({"port": 0, "roles": [
        {"role": "reviewer", "agent": "claude", "worktree": str(wt)},
        {"role": "implementer", "agent": "codex", "worktree": str(wt.parent / "codex")},
        {"role": "tester", "agent": "gemini", "worktree": str(wt.parent / "gemini")},
    ]}))
    (wt.parent / "codex").mkdir(exist_ok=True)
    (wt.parent / "gemini").mkdir(exist_ok=True)

    monkeypatch.setattr(sv, "_claude_home", lambda home=None: tmp_path / "claudehome")
    monkeypatch.setattr(sv, "_pane_alive_for_push", lambda p: True)
    monkeypatch.setattr(sv, "_pane_dismiss_permission_prompt", lambda p: None)
    monkeypatch.setattr(sv.subprocess, "run", lambda *a, **k: _pane())
    monkeypatch.setattr(sv.time, "sleep", lambda *_a: None)
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")

    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, state_path=str(state),
                     pane_map={"claude": "%2", "reviewer": "%2"}, port=0,
                     push_fn=lambda p, t: None, watchdog_disabled=True,
                     anomaly_disabled=True)
    with TestClient(app) as client:
        client.post("/tasks", json={
            "task_id": "t-299r", "task_type": "discuss", "description": "d",
            "branch": "main", "priority": 3, "project": "demo",
            "context": {"agent": "claude"}})

    events = _cleared_events(db)
    assert len(events) == 1, events
    assert events[0]["role"] == "reviewer", events[0]["role"]


def test_an_ambiguous_agent_emits_no_role_rather_than_a_guess():
    """⛔When one provider holds several roles there is no single answer, and
    naming one would be a guess in an audit record. Absent is the honest value
    — the same call this repo already makes for the worktree lookup."""
    live = {"implementer": "claude", "reviewer": "claude", "tester": "gemini"}
    assert sv._agent_role("claude", live) == ""
    assert sv._agent_role("gemini", live) == "tester"


def test_the_role_lookup_falls_back_to_the_static_default():
    assert sv._agent_role("codex", None) == "reviewer"


def test_an_unknown_agent_has_no_role():
    assert sv._agent_role("nobody", {"implementer": "claude"}) == ""
