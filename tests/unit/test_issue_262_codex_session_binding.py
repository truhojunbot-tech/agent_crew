"""#262 — `codex exec resume --last` is global, so it can attach another
project's provider state.

Codex keys its rollout store by DATE, not by working directory, so "the most
recent recorded session" is whatever ran last anywhere on the host. Measured
2026-09-04 on this fleet: 9,894 sessions across many directories, the newest
belonging to `worktrees/alpha_engine/codex` while `worktrees/agent_crew/codex`'s
newest was two days older.

A resume dispatched for agent_crew would therefore have resumed alpha_engine's
session while attribution still recorded agent_crew's `context_id` — a silent
identity mismatch, token cost attributed to the wrong logical context, and
another project's history in the prompt. The task would still have succeeded,
which is what made it invisible.

Each rollout's first record is a `session_meta` carrying `cwd` and `id`, and
`codex exec resume <SESSION_ID>` targets one exactly. That is the binding.
"""

import json
import uuid

import pytest

from agent_crew import server as sv

A = "/wt/project-a/codex"
B = "/wt/project-b/codex"


def _rollout(home, day, stamp, cwd, session_id=None):
    """Write a codex rollout file the way the CLI lays them out."""
    sid = session_id or str(uuid.uuid4())
    d = home / "sessions" / day.replace("-", "/")
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"rollout-{stamp}-{sid}.jsonl"
    meta = {"ordinal": 0, "timestamp": stamp, "type": "session_meta",
            "payload": {"id": sid, "cwd": cwd, "originator": "codex-exec"}}
    f.write_text(json.dumps(meta) + "\n")
    return sid


# ── 1. the binding ────────────────────────────────────────────────────


def test_the_session_for_a_worktree_is_found_by_its_cwd(tmp_path):
    a = _rollout(tmp_path, "2026-09-01", "2026-09-01T10-00-00", A)

    assert sv.codex_session_for_cwd(A, home=tmp_path) == a


def test_a_newer_session_from_another_project_is_not_returned(tmp_path):
    """★The bug: `--last` would pick B, which is newest globally."""
    a = _rollout(tmp_path, "2026-09-01", "2026-09-01T10-00-00", A)
    b = _rollout(tmp_path, "2026-09-04", "2026-09-04T05-52-00", B)

    assert sv.codex_session_for_cwd(A, home=tmp_path) == a
    assert sv.codex_session_for_cwd(B, home=tmp_path) == b
    assert a != b


def test_the_newest_session_for_that_worktree_wins(tmp_path):
    _rollout(tmp_path, "2026-09-01", "2026-09-01T10-00-00", A)
    newer = _rollout(tmp_path, "2026-09-03", "2026-09-03T09-00-00", A)

    assert sv.codex_session_for_cwd(A, home=tmp_path) == newer


def test_a_worktree_with_no_session_binds_to_nothing(tmp_path):
    _rollout(tmp_path, "2026-09-04", "2026-09-04T05-52-00", B)

    assert sv.codex_session_for_cwd(A, home=tmp_path) == ""


@pytest.mark.parametrize("broken", ["not json", '{"payload": {}}', ""])
def test_an_unreadable_rollout_is_skipped_not_fatal(tmp_path, broken):
    d = tmp_path / "sessions" / "2026" / "09" / "04"
    d.mkdir(parents=True)
    (d / "rollout-2026-09-04T06-00-00-broken.jsonl").write_text(broken)
    good = _rollout(tmp_path, "2026-09-01", "2026-09-01T10-00-00", A)

    assert sv.codex_session_for_cwd(A, home=tmp_path) == good


def test_a_missing_store_resolves_to_nothing(tmp_path):
    assert sv.codex_session_for_cwd(A, home=tmp_path / "nope") == ""
    assert sv.codex_session_for_cwd("", home=tmp_path) == ""


def test_the_search_is_bounded(tmp_path):
    """⛔The store is unbounded — 9,894 files on this host — and this runs on
    every codex dispatch. A miss must cost a bounded scan, and a miss means
    fresh, which is safe; an unbounded scan on the dispatch path is not."""
    # ⛔All in ONE day directory, with A's rollout the oldest in it. The
    #   collection loop extends a whole day at a time, so a day larger than the
    #   budget is exactly the case where the READ bound has to do the work — a
    #   version of this test that put A in an older day passed even with the
    #   read bound removed, because the day-level break already excluded it.
    for i in range(30):
        _rollout(tmp_path, "2026-09-04", f"2026-09-04T{i + 1:02d}-00-00", B)
    _rollout(tmp_path, "2026-09-04", "2026-09-04T00-00-00", A)

    assert sv.codex_session_for_cwd(A, home=tmp_path, limit=5) == ""
    assert sv.codex_session_for_cwd(A, home=tmp_path, limit=200) != ""


# ── 2. the dispatch ───────────────────────────────────────────────────


def _dispatch(tmp_path, monkeypatch, *, policy="resume", bound_session=""):
    """One real dispatch of a codex review task; returns (argv, attribution)."""
    import asyncio

    from fastapi.testclient import TestClient

    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue
    from agent_crew.server import create_app

    wt = tmp_path / "codex"
    wt.mkdir(exist_ok=True)
    (wt / ".git").mkdir(exist_ok=True)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"worktrees": {"codex": str(wt)}}))
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
    monkeypatch.setattr(sv, "codex_session_for_cwd", lambda cwd, **kw: bound_session)

    app = create_app(db_path=db, pane_map={}, port=0, state_path=str(state),
                     project="p", watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        q = TaskQueue(db)

        def _one(tid, ctx):
            q.enqueue(TaskRequest(task_id=tid, task_type="review",
                                  description="review it", branch="main", context=ctx))
            task = q.dequeue(role="reviewer")
            assert task is not None
            asyncio.run(app.state.dispatch_task(task, "reviewer"))

        tag = uuid.uuid4().hex[:6]
        # The first dispatch into a triple is always fresh (#202), so seed one.
        _one(f"seed-{tag}", {})
        _one(f"t-{tag}", {"context_reset": True} if policy == "fresh" else {})
        attribution = q.get_attribution(f"t-{tag}") or {}
    return spawned.get("cmd", []), attribution


def test_resume_targets_the_bound_session_by_id(tmp_path, monkeypatch):
    """★★The fix, at dispatch level: an explicit id, never `--last`."""
    cmd, _ = _dispatch(tmp_path, monkeypatch, bound_session="sess-a")

    assert cmd[:4] == ["codex", "exec", "resume", "sess-a"]
    assert "--last" not in cmd, "the global selector is still in the command"


def test_no_binding_means_fresh_rather_than_a_guess(tmp_path, monkeypatch):
    """⛔#262's rule 4: when nothing is known, start fresh. Resuming whatever
    ran last on the host is the failure, not the fallback."""
    cmd, _ = _dispatch(tmp_path, monkeypatch, bound_session="")

    assert cmd[:2] == ["codex", "exec"]
    assert "resume" not in cmd and "--last" not in cmd


def test_a_reset_context_never_resumes(tmp_path, monkeypatch):
    """#261's rule survives: fresh/reset resumes no provider state at all,
    even when a perfectly good binding exists."""
    cmd, _ = _dispatch(tmp_path, monkeypatch, policy="fresh", bound_session="sess-a")

    assert "resume" not in cmd and "sess-a" not in cmd


def test_the_resumed_session_is_recorded_as_provider_session_id(tmp_path, monkeypatch):
    """#262's economics requirement: a resume must be joinable to the exact
    provider session that was resumed."""
    _, attribution = _dispatch(tmp_path, monkeypatch, bound_session="sess-a")

    assert attribution.get("provider_session_id") == "sess-a"
    assert attribution.get("context_policy") == "resume"


def test_an_unknown_session_stays_unknown_in_attribution(tmp_path, monkeypatch):
    """⛔Never attributed by cwd or timing. Unknown reads as unknown."""
    _, attribution = _dispatch(tmp_path, monkeypatch, bound_session="")

    assert not attribution.get("provider_session_id")


def test_the_binding_is_recorded_at_dispatch_not_only_afterwards(tmp_path, monkeypatch):
    """⛔The attribution row is written BEFORE the provider runs. A task that
    crashes mid-run must still be joinable to the session it resumed, so the
    dispatch-time record has to carry it — the post-run capture is a top-up,
    not the source.

    The seed dispatch is `fresh` by #202 (first into the triple) so it makes NO
    dispatch-time lookup: the calls are seed post-run, graded dispatch-time,
    graded post-run. Only the second answers, so the context row never learns
    the id from a post-run capture and the attribution can only have come from
    the dispatch-time record.
    """
    import asyncio

    from fastapi.testclient import TestClient

    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue
    from agent_crew.server import create_app

    wt = tmp_path / "codex"
    wt.mkdir(exist_ok=True)
    (wt / ".git").mkdir(exist_ok=True)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"worktrees": {"codex": str(wt)}}))
    db = str(tmp_path / "t.db")

    async def _fake_exec(*cmd, **kwargs):
        class _P:
            returncode = 0
            pid = 1

            async def communicate(self):
                return (b"", b"")

            async def wait(self):
                return 0

        return _P()

    answers = iter(["", "sess-a", ""])
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(sv, "codex_session_for_cwd", lambda cwd, **kw: next(answers, ""))

    app = create_app(db_path=db, pane_map={}, port=0, state_path=str(state),
                     project="p", watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        q = TaskQueue(db)
        seed = f"seed-{uuid.uuid4().hex[:6]}"
        q.enqueue(TaskRequest(task_id=seed, task_type="review", description="r",
                              branch="main", context={}))
        asyncio.run(app.state.dispatch_task(q.dequeue(role="reviewer"), "reviewer"))

        tid = f"t-{uuid.uuid4().hex[:6]}"
        q.enqueue(TaskRequest(task_id=tid, task_type="review", description="r",
                              branch="main", context={}))
        asyncio.run(app.state.dispatch_task(q.dequeue(role="reviewer"), "reviewer"))
        attribution = q.get_attribution(tid) or {}

    assert attribution.get("provider_session_id") == "sess-a", (
        "the session was only recorded after the run — a crashed task would be "
        "unjoinable to the context it resumed"
    )


def test_project_b_cannot_be_attached_to_project_a(tmp_path, monkeypatch):
    """★★The A/B interleaving the issue asks for, at dispatch level.

    B runs last, so `--last` would select B. A's dispatch must still resume A.
    """
    home = tmp_path / "codexhome"
    a = _rollout(home, "2026-09-01", "2026-09-01T10-00-00", str(tmp_path / "codex"))
    b = _rollout(home, "2026-09-04", "2026-09-04T05-52-00", B)

    real = sv.codex_session_for_cwd
    cmd, attribution = _dispatch(tmp_path, monkeypatch,
                                 bound_session=real(str(tmp_path / "codex"), home=home))

    assert a != b
    assert cmd[3] == a, "the dispatch resumed another project's session"
    assert attribution.get("provider_session_id") == a


# ── the search must not walk the whole store (review of PR #266) ──────
#
# `sorted(root.rglob("*"), reverse=True)` materialised the entire tree before
# the read budget applied. Measured on the real store: 10,028 paths in 167ms,
# on every unresolved resume AND every post-run capture. The read budget
# bounded the file reads that followed and nothing else.


def _big_store(home, years=("2026", "2025", "2024"), months=12, days=28,
               match_cwd=None):
    """A store shaped like the real one: sessions/YYYY/MM/DD/rollout-*.jsonl."""
    newest = None
    for y in years:
        for m in range(months, 0, -1):
            for d in range(days, 0, -1):
                day = home / "sessions" / y / f"{m:02d}" / f"{d:02d}"
                day.mkdir(parents=True, exist_ok=True)
                sid = f"{y}{m:02d}{d:02d}-session"
                cwd = match_cwd if (y, m, d) == (years[0], months, days) else B
                (day / f"rollout-{y}-{m:02d}-{d:02d}T00-00-00-{sid}.jsonl").write_text(
                    json.dumps({"payload": {"id": sid, "cwd": cwd}}) + "\n")
                if newest is None:
                    newest = sid
    return newest


def _count_listings(monkeypatch):
    """Count directory listings, which is what a full traversal actually costs."""
    import pathlib

    seen = []
    real = pathlib.Path.iterdir

    def counting(self):
        seen.append(str(self))
        return real(self)

    monkeypatch.setattr(pathlib.Path, "iterdir", counting)
    return seen


def test_a_hit_lists_only_the_newest_path_through_the_tree(tmp_path, monkeypatch):
    """★★The regression the review asked for: traversal, not reads.

    A store of 3 years × 12 months × 28 days is 1,008 day directories. Reaching
    the newest session must list the year, month and day levels once each — not
    enumerate the tree.
    """
    newest = _big_store(tmp_path, match_cwd=A)
    listings = _count_listings(monkeypatch)

    assert sv.codex_session_for_cwd(A, home=tmp_path) == newest

    assert len(listings) <= 4, (
        f"reaching the newest session listed {len(listings)} directories; the "
        f"date hierarchy should need one listing per level"
    )


def test_a_miss_does_not_enumerate_the_whole_store(tmp_path, monkeypatch):
    """⛔A miss is the expensive case and the common one on a worktree that has
    not run codex recently. It must cost the read budget, not the store."""
    _big_store(tmp_path, match_cwd=B)          # nothing matches A
    listings = _count_listings(monkeypatch)

    assert sv.codex_session_for_cwd(A, home=tmp_path, limit=5) == ""

    # One file per day here, so a budget of 5 reaches at most a handful of days
    # plus the levels above them. The point is the bound, not the exact number.
    assert len(listings) <= 20, (
        f"a miss listed {len(listings)} directories for a budget of 5 — the "
        f"traversal is not bounded by the budget"
    )


def test_the_day_walk_is_lazy(tmp_path):
    """The generator must not be drained to yield its first item — that is the
    property the fix rests on, and a list comprehension would pass every other
    test in this file while restoring the defect."""
    import itertools

    _big_store(tmp_path, match_cwd=A)
    root = tmp_path / "sessions"

    first_two = list(itertools.islice(sv._codex_day_dirs(root), 2))

    assert len(first_two) == 2
    assert first_two[0].name == "28" and first_two[0].parent.name == "12"


def test_an_unshaped_store_still_resolves(tmp_path):
    """⛔The lazy walk assumes YYYY/MM/DD. A store that is flat, or shallower,
    must still be searched rather than silently yielding nothing."""
    day = tmp_path / "sessions"
    day.mkdir(parents=True)
    sid = "flat-session"
    (day / f"rollout-2026-09-04T00-00-00-{sid}.jsonl").write_text(
        json.dumps({"payload": {"id": sid, "cwd": A}}) + "\n")

    assert sv.codex_session_for_cwd(A, home=tmp_path) == sid
