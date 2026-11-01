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

⛔`send-keys` returning 0 proves the keystrokes were delivered, not that the
  provider cleared. An unconfirmed send remains `attempted` and does not set
  `context_reset`; a measured drop after the send confirms the reset.
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


def _run(tmp_path, monkeypatch, *, tokens=BIG, post_clear_tokens=None, task_type="implement",
         context=None, agent_key="claude", pane="%91", unused_tcp_port):
    """Push one task through the real path; return (db, sent_keys, task_ctx)."""
    wt = tmp_path / "worktrees" / "demo" / agent_key
    wt.mkdir(parents=True, exist_ok=True)
    _session(tmp_path / "claudehome", str(wt), tokens)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"port": 0, "roles": [
        {"role": "implementer", "agent": agent_key, "worktree": str(wt)},
        {"role": "reviewer", "agent": "codex", "worktree": str(wt.parent / "codex")},
        {"role": "tester", "agent": "gemini", "worktree": str(wt.parent / "gemini")},
    ]}))

    sent = []

    def fake_run(cmd, **kw):
        if isinstance(cmd, list) and "send-keys" in cmd:
            sent.append(cmd)
            if "/clear" in cmd and post_clear_tokens is not None:
                _session(tmp_path / "claudehome", str(wt), post_clear_tokens)
        return _pane()

    monkeypatch.setattr(sv, "_claude_home", lambda home=None: tmp_path / "claudehome")
    monkeypatch.setattr(sv, "_pane_alive_for_push", lambda p: True)
    monkeypatch.setattr(sv, "_pane_dismiss_permission_prompt", lambda p: None)
    monkeypatch.setattr(sv.subprocess, "run", fake_run)
    monkeypatch.setattr(sv.time, "sleep", lambda *_a: None)
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")

    db = str(tmp_path / "tasks.db")
    pane_map = {"implementer": pane, "claude": pane, "codex": pane,
                "reviewer": pane, "tester": pane}
    app = create_app(db_path=db, state_path=str(state), pane_map=pane_map,
                     port=unused_tcp_port, push_fn=lambda p, t: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        client.post("/tasks", json={
            "task_id": "t-297", "task_type": task_type, "description": "go",
            "branch": "main", "priority": 3, "project": "demo",
            "context": context or {}})
        ctx = TaskQueue(db).get_task_context("t-297")
    return db, sent, ctx


# ── 1. the clear leaves a durable record ──────────────────────────────














# ── 2. the treatment is corrected, not just described ─────────────────














# ── 3. both push paths ────────────────────────────────────────────────






# ── 4. identity, where the push path can know it ──────────────────────






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


def _failing_send(tmp_path, monkeypatch, *, task_type="implement", context=None, unused_tcp_port):
    """Same push, but tmux refuses the keystrokes."""
    wt = tmp_path / "worktrees" / "demo" / "claude"
    wt.mkdir(parents=True, exist_ok=True)
    _session(tmp_path / "claudehome", str(wt), BIG)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"port": 0, "roles": [
        {"role": "implementer", "agent": "claude", "worktree": str(wt)},
        {"role": "reviewer", "agent": "codex", "worktree": str(wt.parent / "codex")},
        {"role": "tester", "agent": "gemini", "worktree": str(wt.parent / "gemini")},
    ]}))

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
    monkeypatch.setattr(sv, "_pane_dismiss_permission_prompt", lambda p: None)
    monkeypatch.setattr(sv.subprocess, "run", fake_run)
    monkeypatch.setattr(sv.time, "sleep", lambda *_a: None)
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")

    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, state_path=str(state),
                     pane_map={"implementer": "%91", "claude": "%91"}, port=unused_tcp_port,
                     push_fn=lambda p, t: None, watchdog_disabled=True,
                     anomaly_disabled=True)
    with TestClient(app) as client:
        client.post("/tasks", json={
            "task_id": "t-299", "task_type": task_type, "description": "go",
            "branch": "main", "priority": 3, "project": "demo",
            "context": context or {}})
        ctx = TaskQueue(db).get_task_context("t-299")
    return db, ctx










# ── 6. the event's role comes from the live map ───────────────────────




def test_an_ambiguous_agent_emits_no_role_rather_than_a_guess():
    """⛔When one provider holds several roles there is no single answer, and
    naming one would be a guess in an audit record. Absent is the honest value
    — the same call this repo already makes for the worktree lookup."""
    live = {"implementer": "claude", "reviewer": "claude", "tester": "gemini"}
    assert sv._agent_role("claude", live) == ""
    assert sv._agent_role("gemini", live) == "tester"


def test_the_role_lookup_falls_back_to_the_static_default():
    assert sv._agent_role("codex", None) == "implementer"


def test_an_unknown_agent_has_no_role():
    assert sv._agent_role("nobody", {"implementer": "claude"}) == ""
