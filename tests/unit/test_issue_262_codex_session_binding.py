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


def _pad(home, session_id, extra_bytes):
    """Grow a rollout WITHOUT destroying its `session_meta` first line.

    Overwriting the file removes the cwd binding, so the lookup stops finding
    it and the cap silently cannot trip — which is how a first version of these
    tests "proved" the cap did not work.
    """
    path = next((home / "sessions").rglob(f"*{session_id}.jsonl"))
    with open(path, "a") as fh:
        fh.write("x" * extra_bytes)
    return path


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


# ── codex gets a size cap too, now that a resume is bound (#260 review) ──
#
# The stated limitation — "codex has no per-worktree size signal" — was true
# while `resume --last` was global. Binding a resume to ONE session by id made
# it measurable, and the reviewer caught that the note had gone stale.
#
# Measured 2026-09-06 across 9,894 rollouts: median 48 KB, p99 0.4 MB — and
# alpha_engine's codex worktree holding 356.6 / 180.6 / 130.3 / 114.7 MB files.


def test_the_bound_rollout_is_what_gets_measured(tmp_path):
    """★The size is of the file a resume would actually replay, not the store."""
    sid = _rollout(tmp_path, "2026-09-04", "2026-09-04T10-00-00", A)
    path = _pad(tmp_path, sid, 5000)
    _rollout(tmp_path, "2026-09-04", "2026-09-04T09-00-00", A)   # older, unused

    size, session = sv.codex_session_size(A, home=tmp_path)
    assert session == sid
    assert size == path.stat().st_size >= 5000


def test_a_rollout_over_the_cap_trips(tmp_path):
    _rollout(tmp_path, "2026-09-04", "2026-09-04T10-00-00", A, session_id="big")
    _pad(tmp_path, "big", 3 * 1024 * 1024)

    over, info = sv.codex_context_exceeds_cap(A, max_mb=2, home=tmp_path)

    assert over is True
    assert info["provider"] == "codex" and info["conversation_id"] == "big"
    assert info["bytes"] > 3 * 1024 * 1024


def test_a_rollout_under_the_cap_does_not(tmp_path):
    _rollout(tmp_path, "2026-09-04", "2026-09-04T10-00-00", A)

    assert sv.codex_context_exceeds_cap(A, max_mb=2, home=tmp_path)[0] is False


@pytest.mark.parametrize("cap", [0, -1])
def test_a_zero_cap_disables_the_codex_check(tmp_path, cap):
    _rollout(tmp_path, "2026-09-04", "2026-09-04T10-00-00", A, session_id="big")
    _pad(tmp_path, "big", 9 * 1024 * 1024)

    assert sv.codex_context_exceeds_cap(A, max_mb=cap, home=tmp_path)[0] is False


def test_an_unbound_worktree_is_not_over_the_cap(tmp_path):
    """No session means nothing to resume — fresh, not oversized."""
    assert sv.codex_context_exceeds_cap(A, max_mb=1, home=tmp_path)[0] is False


def _codex_dispatch(tmp_path, monkeypatch, *, over, bound="sess-a"):
    """A real codex dispatch with the cap decision stubbed; returns (argv, event)."""
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
    monkeypatch.setattr(sv, "codex_session_for_cwd", lambda cwd, **kw: bound)
    monkeypatch.setattr(sv, "codex_context_exceeds_cap",
                        lambda cwd, *a, **k: (over, {"bytes": 99 * 1048576,
                                                     "conversation_id": bound,
                                                     "cap_mb": 64,
                                                     "provider": "codex"}))

    app = create_app(db_path=db, pane_map={}, port=0, state_path=str(state),
                     project="p", watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        q = TaskQueue(db)
        for tid in (f"seed-{uuid.uuid4().hex[:6]}", f"t-{uuid.uuid4().hex[:6]}"):
            q.enqueue(TaskRequest(task_id=tid, task_type="review", description="r",
                                  branch="main", context={}))
            asyncio.run(app.state.dispatch_task(q.dequeue(role="reviewer"), "reviewer"))

    events = [json.loads(l) for l in open(tmp_path / "context_events.jsonl")]
    capped = [e for e in events if e["event_type"] == "provider_context_capped"]
    return spawned.get("cmd", []), (capped[-1] if capped else None)


def test_an_over_cap_codex_session_is_not_resumed(tmp_path, monkeypatch):
    """★★End to end: over the cap → fresh, and the event names codex."""
    cmd, event = _codex_dispatch(tmp_path, monkeypatch, over=True)

    assert "resume" not in cmd, "an oversized rollout was resumed anyway"
    assert cmd[:2] == ["codex", "exec"]
    assert event is not None and event["provider"] == "codex"
    assert event["bytes"] == 99 * 1048576 and event["cap_mb"] == 64


def test_an_under_cap_codex_session_still_resumes(tmp_path, monkeypatch):
    """⛔The cap must not become a blanket refusal to resume."""
    cmd, event = _codex_dispatch(tmp_path, monkeypatch, over=False)

    assert cmd[:4] == ["codex", "exec", "resume", "sess-a"]
    assert event is None


# ── the cap must measure the session that will be RESUMED (#260 review) ──
#
# The cap measured "newest rollout for this cwd" while the resume preferred the
# durable `provider_session_id`, which can be an OLDER rollout. Two failures
# from one mismatch: an oversized stored session resumes uncapped because a
# small newer file was measured, and a healthy stored session gets reset
# because an unrelated newer rollout happened to be large.


def test_the_size_is_read_for_a_specific_session(tmp_path):
    old_sid = _rollout(tmp_path, "2026-09-01", "2026-09-01T10-00-00", A, session_id="older")
    _pad(tmp_path, "older", 3 * 1024 * 1024)
    _rollout(tmp_path, "2026-09-04", "2026-09-04T10-00-00", A, session_id="newer")

    assert sv.codex_session_size_for_id("older", home=tmp_path) > 3 * 1024 * 1024
    assert sv.codex_session_size_for_id("newer", home=tmp_path) < 1024
    assert sv.codex_session_size_for_id("nope", home=tmp_path) == 0


def test_an_oversized_stored_session_trips_even_when_the_newest_is_small(tmp_path):
    """★The dangerous half: the resume would replay the big one."""
    _rollout(tmp_path, "2026-09-01", "2026-09-01T10-00-00", A, session_id="stored")
    _pad(tmp_path, "stored", 3 * 1024 * 1024)
    _rollout(tmp_path, "2026-09-04", "2026-09-04T10-00-00", A, session_id="newest")

    assert sv.codex_context_exceeds_cap(A, max_mb=2, home=tmp_path)[0] is False
    over, info = sv.codex_context_exceeds_cap(A, max_mb=2, home=tmp_path,
                                              session_id="stored")

    assert over is True and info["conversation_id"] == "stored"


def test_a_healthy_stored_session_is_not_reset_by_an_unrelated_big_rollout(tmp_path):
    """⛔The other half, and the one that would have looked like the cap
    working: a large newer rollout for the same cwd is not what a resume would
    replay, so it must not force a reset of a perfectly good session."""
    _rollout(tmp_path, "2026-09-01", "2026-09-01T10-00-00", A, session_id="stored")
    _rollout(tmp_path, "2026-09-04", "2026-09-04T10-00-00", A, session_id="huge")
    _pad(tmp_path, "huge", 5 * 1024 * 1024)

    assert sv.codex_context_exceeds_cap(A, max_mb=2, home=tmp_path)[0] is True
    assert sv.codex_context_exceeds_cap(A, max_mb=2, home=tmp_path,
                                        session_id="stored")[0] is False


def test_the_peek_does_not_mint_a_context(tmp_db):
    """⛔The cap decision has to ask which session before deciding, and
    `get_or_create_context` cannot answer a question — it mints an id, bumps
    the generation and increments the task index."""
    from agent_crew.queue import TaskQueue

    q = TaskQueue(tmp_db)

    assert q.peek_context_provider_session_id("p", "codex", "/wt") == ""
    ctx = q.get_or_create_context(project="p", agent="codex", worktree_path="/wt",
                                  role="reviewer", task_id="t-1")
    assert ctx["context_generation"] == 1
    q.update_context_provider_session_id(ctx["context_key"], "stored-id")

    assert q.peek_context_provider_session_id("p", "codex", "/wt") == "stored-id"
    # ...and asking again did not advance anything.
    again = q.get_or_create_context(project="p", agent="codex", worktree_path="/wt",
                                    role="reviewer", task_id="t-2")
    assert again["context_generation"] == 1
    assert again["session_task_index"] == 2      # only the real dispatch advanced it


def test_dispatch_measures_the_stored_session_not_the_newest(tmp_path, monkeypatch):
    """★★The regression the review asked for: an older stored oversized id plus
    a newer under-cap rollout for the same cwd."""
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
    home = tmp_path / "codexhome"

    _rollout(home, "2026-09-01", "2026-09-01T10-00-00", str(wt), session_id="stored")
    _pad(home, "stored", 3 * 1024 * 1024)
    _rollout(home, "2026-09-04", "2026-09-04T10-00-00", str(wt), session_id="newest")

    measured = {}
    real_cap = sv.codex_context_exceeds_cap

    def cap(cwd, *a, **kw):
        measured["session_id"] = kw.get("session_id")
        return real_cap(cwd, max_mb=2, home=home, session_id=kw.get("session_id", ""))

    async def _fake_exec(*cmd, **kwargs):
        measured["cmd"] = list(cmd)

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
    monkeypatch.setattr(sv, "codex_context_exceeds_cap", cap)
    monkeypatch.setattr(sv, "codex_session_for_cwd",
                        lambda cwd, **kw: sv.codex_session_for_cwd.__wrapped__(cwd)
                        if hasattr(sv.codex_session_for_cwd, "__wrapped__") else "newest")

    app = create_app(db_path=db, pane_map={}, port=0, state_path=str(state),
                     project="p", watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        q = TaskQueue(db)
        seed = f"seed-{uuid.uuid4().hex[:6]}"
        q.enqueue(TaskRequest(task_id=seed, task_type="review", description="r",
                              branch="main", context={}))
        asyncio.run(app.state.dispatch_task(q.dequeue(role="reviewer"), "reviewer"))
        # The durable binding names the OLD oversized rollout. Read the row the
        # DISPATCHER created rather than minting one: it resolves `project` from
        # the state directory (#248), so a hand-built key would be a different
        # context and the peek would miss it — which is exactly what this test
        # caught on its first run.
        import sqlite3 as _sqlite

        conn = _sqlite.connect(db)
        conn.row_factory = _sqlite.Row
        row = conn.execute(
            "SELECT context_key FROM context_state WHERE agent='codex' "
            "AND worktree_path=?", (str(wt),)).fetchone()
        conn.close()
        assert row is not None, "the seed dispatch did not create a context row"
        q.update_context_provider_session_id(row["context_key"], "stored")

        tid = f"t-{uuid.uuid4().hex[:6]}"
        q.enqueue(TaskRequest(task_id=tid, task_type="review", description="r",
                              branch="main", context={}))
        asyncio.run(app.state.dispatch_task(q.dequeue(role="reviewer"), "reviewer"))

    assert measured.get("session_id") == "stored", (
        f"the cap measured {measured.get('session_id')!r}, not the session the "
        f"resume would replay"
    )
    assert "resume" not in measured["cmd"], "an oversized stored session was resumed"
