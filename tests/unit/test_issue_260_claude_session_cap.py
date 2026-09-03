"""#260 — claude/codex workers resumed forever with no cap.

#236/#238 bounded the agy store. Nothing bounded the other two providers:
`--continue` was unconditional for claude and `resume --last` unconditional for
codex, so their sessions never rotated at all. Measured on this host
2026-09-03 — every crew worktree had exactly ONE session file since 2026-08-21,
alpha_engine's at 290 MB — and Quota measured quota-ops above 900k cached
tokens on 81 of 1095 turns.

The file size is the store, not the context window (Claude Code compacts
internally). It is the signal observable from outside the CLI, and a store that
never rotates is what keeps the window pinned near its ceiling.
"""

import json
import os

import pytest

from agent_crew import server as sv


def _session(home, cwd, name="0aff70cb-dd90", size=1024, mtime=None):
    import re
    d = home / "projects" / re.sub(r"[/._]", "-", cwd)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.jsonl"
    f.write_bytes(b"x" * size)
    if mtime:
        os.utime(f, (mtime, mtime))
    return f


CWD = "/home/u/.agent_crew/worktrees/quota-ops/claude"


# ── 1. the size signal ────────────────────────────────────────────────


def test_the_session_is_found_through_claude_codes_path_mangling(tmp_path):
    """★`/`, `.` AND `_` all fold to `-`. Getting this wrong finds nothing and
    the cap silently never fires — the failure mode is a quiet no-op."""
    _session(tmp_path, CWD, size=5000)

    size, session = sv.claude_session_size(CWD, home=tmp_path)

    assert size == 5000 and session == "0aff70cb-dd90"
    expected = tmp_path / "projects" / "-home-u--agent-crew-worktrees-quota-ops-claude"
    assert expected.is_dir(), "the mangling rule changed"


def test_the_most_recent_session_is_the_one_continue_would_resume(tmp_path):
    _session(tmp_path, CWD, name="old", size=9000, mtime=1_000_000)
    _session(tmp_path, CWD, name="new", size=10, mtime=2_000_000)

    assert sv.claude_session_size(CWD, home=tmp_path) == (10, "new")


@pytest.mark.parametrize("setup", ["missing dir", "empty dir", "no cwd"])
def test_an_unreadable_store_sizes_to_zero_rather_than_raising(tmp_path, setup):
    """⛔Sizing runs on every dispatch; it may never be the thing that breaks one."""
    if setup == "empty dir":
        import re
        (tmp_path / "projects" / re.sub(r"[/._]", "-", CWD)).mkdir(parents=True)
    cwd = "" if setup == "no cwd" else CWD

    assert sv.claude_session_size(cwd, home=tmp_path) == (0, "")


# ── 2. the cap ────────────────────────────────────────────────────────


def test_a_session_over_the_cap_trips(tmp_path):
    _session(tmp_path, CWD, size=3 * 1024 * 1024)

    over, info = sv.claude_context_exceeds_cap(CWD, max_mb=2, home=tmp_path)

    assert over is True
    assert info["provider"] == "claude" and info["cap_mb"] == 2
    assert info["bytes"] == 3 * 1024 * 1024


def test_a_session_under_the_cap_does_not(tmp_path):
    _session(tmp_path, CWD, size=1024)

    assert sv.claude_context_exceeds_cap(CWD, max_mb=2, home=tmp_path)[0] is False


@pytest.mark.parametrize("cap", [0, -1])
def test_a_zero_cap_disables_the_check(tmp_path, cap):
    _session(tmp_path, CWD, size=50 * 1024 * 1024)

    assert sv.claude_context_exceeds_cap(CWD, max_mb=cap, home=tmp_path)[0] is False


def test_no_session_is_not_over_the_cap(tmp_path):
    """A worker with no session yet is fresh, not oversized."""
    assert sv.claude_context_exceeds_cap(CWD, max_mb=1, home=tmp_path)[0] is False


# ── 3. the dispatch decision ──────────────────────────────────────────


def _dispatch_cmd(tmp_path, monkeypatch, agent, *, policy="resume", over=False):
    """Run one real dispatch and return the argv the dispatcher would spawn."""
    import asyncio

    from fastapi.testclient import TestClient

    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue
    from agent_crew.server import create_app

    wt = tmp_path / agent
    wt.mkdir(exist_ok=True)      # the same tmp_path is reused across calls
    (wt / ".git").mkdir(exist_ok=True)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"worktrees": {agent: str(wt)}}))
    db = str(tmp_path / "t.db")
    spawned = {}

    async def _fake_exec(*cmd, **kwargs):
        spawned["cmd"] = list(cmd)

        class _P:
            returncode = 0
            pid = 1

            async def communicate(self):
                return (b"", b"")

            async def wait(self):
                return 0

        return _P()

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(sv, "claude_context_exceeds_cap",
                        lambda cwd, *a, **k: (over, {"bytes": 99 * 1048576,
                                                     "conversation_id": "sess",
                                                     "cap_mb": 64,
                                                     "provider": "claude"}))
    monkeypatch.setattr(sv, "agy_context_exceeds_cap", lambda cwd, *a, **k: (False, {}))

    role = {"claude": "implementer", "codex": "reviewer"}[agent]
    ttype = {"claude": "implement", "codex": "review"}[agent]
    app = create_app(db_path=db, pane_map={}, port=0, state_path=str(state),
                     project="p", watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        q = TaskQueue(db)

        def _one(task_id, ctx):
            q.enqueue(TaskRequest(task_id=task_id, task_type=ttype,
                                  description="do it", branch="main", context=ctx))
            task = q.dequeue(role=role)
            assert task is not None
            asyncio.run(app.state.dispatch_task(task, role))

        # ⛔The FIRST dispatch into a (project, agent, worktree) triple is
        #   always `fresh` by #202's design — there is no prior context to
        #   resume. Testing the resume path therefore needs a second dispatch,
        #   and a harness that skips this measures the wrong policy.
        import uuid as _uuid
        tag = _uuid.uuid4().hex[:6]
        _one(f"t-{agent}-seed-{tag}", {})
        _one(f"t-{agent}-{policy}-{tag}",
             {"context_reset": True} if policy == "fresh" else {})
    return spawned.get("cmd", [])


def test_claude_resumes_when_the_policy_says_resume(tmp_path, monkeypatch):
    cmd = _dispatch_cmd(tmp_path, monkeypatch, "claude")

    assert cmd[0] == "claude" and "--continue" in cmd


def test_claude_does_not_resume_after_an_operator_reset(tmp_path, monkeypatch):
    """★The core gap: `--continue` was unconditional, so a freshly minted
    context still resumed the provider's old session."""
    cmd = _dispatch_cmd(tmp_path, monkeypatch, "claude", policy="fresh")

    assert "--continue" not in cmd, "a reset context still resumed the old session"


def test_a_capped_claude_session_forces_a_fresh_one(tmp_path, monkeypatch):
    """★★The cap, end to end: over the limit → no `--continue` on the real argv."""
    cmd = _dispatch_cmd(tmp_path, monkeypatch, "claude", over=True)

    assert cmd[0] == "claude"
    assert "--continue" not in cmd


def test_codex_resume_is_policy_aware_too(tmp_path, monkeypatch):
    resumed = _dispatch_cmd(tmp_path, monkeypatch, "codex")
    fresh = _dispatch_cmd(tmp_path, monkeypatch, "codex", policy="fresh")

    assert resumed[:4] == ["codex", "exec", "resume", "--last"]
    assert "resume" not in fresh and fresh[:2] == ["codex", "exec"]


def test_the_capped_session_is_never_deleted(tmp_path, monkeypatch):
    """⛔Reversible by construction: the oversized session is not resumed, not
    removed. Nothing in the provider's store is mutated."""
    f = _session(tmp_path / "home", CWD, size=4096)
    monkeypatch.setattr(sv, "CLAUDE_CONTEXT_MAX_MB", 0.001)

    sv.claude_context_exceeds_cap(CWD, home=tmp_path / "home")

    assert f.exists() and f.stat().st_size == 4096
