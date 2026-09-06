"""#278 — the tester treatment has to be a field, not a sentence.

PR #275 made the tester's cost depend on a resolved scope: two tasks with the
same `task_type`, role, provider, project and context can now cost radically
different amounts. The merged contract only asked the agent to *say* which
scope it used, in prose. Building quota-core's cohorts on that would make a
measurement contract depend on an LLM's phrasing.

What this pins:

  * the effective treatment, its categorical source and a config fingerprint,
    written to `task_attribution` and to the context-event stream on the real
    dispatch path;
  * lock deferral as scheduler delay that is *separable* from provider runtime;
  * ⛔historical rows staying NULL. "Unknown treatment" and "targeted" are
    different facts, and a column defaulted to either one would quietly
    manufacture a cohort.
"""

import asyncio
import json
import os

import pytest

from agent_crew import testing_policy as tp
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


# ── 1. the treatment identity ─────────────────────────────────────────


def test_the_default_treatment_is_targeted_and_built_in():
    assert tp.effective_scope(tp.DEFAULT_SCOPE) == tp.SCOPE_TARGETED
    assert tp.DEFAULT_SCOPE["source_kind"] == "builtin"


@pytest.mark.parametrize("kind", tp.SOURCE_KINDS)
def test_every_source_kind_is_one_of_the_declared_four(kind):
    assert tp._coerce({}, "/some/path", kind)["source_kind"] == kind


def test_an_unknown_source_kind_degrades_to_builtin():
    """A typo must not invent a fifth cohort."""
    assert tp._coerce({}, "/p", "operatorr")["source_kind"] == "builtin"


def test_the_fingerprint_ignores_where_the_policy_came_from():
    """⛔The same policy reached through the operator file and through the repo
    file is the same treatment. Folding provenance into the hash would split
    one cohort in two, which is the opposite of what a rollout needs."""
    config = {"targeted": ["pytest {paths}"], "guards": ["ruff check ."]}
    assert (tp.scope_fingerprint(tp._coerce(config, "/a", "operator"))
            == tp.scope_fingerprint(tp._coerce(config, "/b", "repo")))


def test_the_fingerprint_changes_when_behaviour_changes():
    base = tp._coerce({"targeted": ["pytest {paths}"]}, "/a", "repo")
    for changed in ({"targeted": ["pytest {paths} -x"]},
                    {"targeted": ["pytest {paths}"], "guards": ["ruff check ."]},
                    {"targeted": ["pytest {paths}"], "full_suite": True}):
        assert tp.scope_fingerprint(tp._coerce(changed, "/a", "repo")) != tp.scope_fingerprint(base)


def test_the_fingerprint_does_not_leak_the_commands():
    """⛔Commands carry absolute worktree paths and project-internal names, and
    this stream is published to the quota systems. #278 asks for a hash for
    exactly that reason."""
    secret = "/home/someone/private-repo/run_tests.sh --token abc123"
    digest = tp.scope_fingerprint(tp._coerce({"targeted": [secret]}, "/a", "repo"))
    assert secret not in digest and "private-repo" not in digest
    assert len(digest) == 16 and all(c in "0123456789abcdef" for c in digest)


def test_a_malformed_override_reports_the_scope_that_was_USED(tmp_path, monkeypatch):
    """★★#278 validation 3. A cohort built on the requested-but-invalid scope
    measures intent; only the effective scope measures cost."""
    monkeypatch.setenv(tp.ENV_SCOPE, '{"full_suite": true,')     # truncated JSON
    scope = tp.load_scope(str(tmp_path), "demo", base=str(tmp_path / "nope"))
    assert tp.effective_scope(scope) == tp.SCOPE_TARGETED
    assert scope["source_kind"] == "builtin"


# ── 2. it survives to the attribution row ─────────────────────────────


def _attr(tmp_db, task_id):
    return TaskQueue(tmp_db).get_attribution(task_id)


def test_a_row_nobody_measured_stays_unknown(tmp_db):
    """★★#278 criterion 5. NULL, not '' and not 0 — "no treatment recorded" and
    "targeted with no lock wait" are different facts about the world."""
    q = TaskQueue(tmp_db)
    q.record_attribution("t-old", project="p", agent="gemini", role="tester",
                         task_type="test")
    row = _attr(tmp_db, "t-old")
    for field in ("effective_test_scope", "test_scope_source", "test_scope_hash",
                  "lock_wait_seconds", "lock_defer_count"):
        assert row[field] is None, f"{field} was defaulted to {row[field]!r}"


def test_recording_the_treatment_fills_exactly_those_fields(tmp_db):
    q = TaskQueue(tmp_db)
    q.record_attribution("t-new", project="p", agent="gemini", role="tester",
                         task_type="test")
    q.record_test_economics("t-new", effective_test_scope=tp.SCOPE_FULL,
                            test_scope_source="operator", test_scope_hash="deadbeefdeadbeef",
                            lock_wait_seconds=12.5, lock_defer_count=3)
    row = _attr(tmp_db, "t-new")
    assert row["effective_test_scope"] == "full_suite"
    assert row["test_scope_source"] == "operator"
    assert row["test_scope_hash"] == "deadbeefdeadbeef"
    assert row["lock_wait_seconds"] == 12.5 and row["lock_defer_count"] == 3


def test_the_treatment_joins_to_context_identity(tmp_db):
    """The whole point is a join. If these fields did not sit on the row that
    carries `context_id`, a cohort could not be built at all."""
    q = TaskQueue(tmp_db)
    q.record_attribution("t-join", project="p", agent="gemini", role="tester",
                         task_type="test", context_id="ctx-1", context_generation=2)
    q.record_test_economics("t-join", effective_test_scope=tp.SCOPE_TARGETED,
                            test_scope_source="repo", test_scope_hash="abc")
    row = _attr(tmp_db, "t-join")
    assert (row["context_id"], row["context_generation"]) == ("ctx-1", 2)
    assert row["effective_test_scope"] == "targeted"


# ── 3. deferral is counted, and it is not runtime ─────────────────────


def _task(q, task_id="test-1"):
    q.enqueue(TaskRequest(task_id=task_id, task_type="test", description="run",
                          branch="main", context={}))


def test_each_deferral_is_counted_and_the_first_one_is_remembered(tmp_db):
    q = TaskQueue(tmp_db)
    _task(q)
    first_count, first_at = q.note_test_lock_defer("test-1")
    second_count, second_at = q.note_test_lock_defer("test-1")
    assert (first_count, second_count) == (1, 2)
    assert second_at == first_at, "the waiting-since anchor moved, so the wait would reset"
    assert first_at > 0


def test_the_counter_survives_in_the_tasks_own_context(tmp_db):
    """⛔It has to outlive the dispatch attempt that observed it. The lock is
    non-blocking, so the wait is spread across N separate attempts — a counter
    in process memory would reset on the restart this is meant to survive."""
    q = TaskQueue(tmp_db)
    _task(q)
    q.note_test_lock_defer("test-1")
    context = TaskQueue(tmp_db).get_task_context("test-1")   # a fresh handle
    assert context["test_lock_defer_count"] == 1
    assert context["test_lock_first_deferred_at"] > 0


def test_deferring_an_unknown_task_is_not_an_error(tmp_db):
    assert TaskQueue(tmp_db).note_test_lock_defer("never-existed") == (0, 0.0)


# ── 4. the real dispatch path ─────────────────────────────────────────


def _dispatch(tmp_path, monkeypatch, *, lock_base, task_id="test-d", scope=None,
              defers=0):
    """One real `_dispatch_task`; returns (spawned?, attribution row, events)."""
    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    spawned = []

    async def _fake_exec(*cmd, **kwargs):
        spawned.append(list(cmd))

        class _P:
            returncode, pid = 0, 1

            async def wait(self):
                return 0

        return _P()

    wt = tmp_path / "worktrees" / "demo" / "gemini"
    wt.mkdir(parents=True, exist_ok=True)
    if scope is not None:
        path = wt / tp.REPO_SCOPE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(scope))
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"port": 0, "worktrees": {"gemini": str(wt)}}))
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setenv("AGENT_CREW_BASE", lock_base)
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec", _fake_exec)

    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, pane_map={}, port=0, state_path=str(state),
                     project="demo", watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        q = TaskQueue(db)
        q.enqueue(TaskRequest(task_id=task_id, task_type="test", description="run",
                              branch="main", context={}))
        for _ in range(defers):
            q.note_test_lock_defer(task_id)   # as earlier, deferred attempts would
        task = q.dequeue(role="tester")
        assert task is not None
        asyncio.run(app.state.dispatch_task(task, "tester"))
        row = q.get_attribution(task_id)
    events = []
    events_path = os.path.join(os.path.dirname(db), "context_events.jsonl")
    if os.path.exists(events_path):
        events = [json.loads(line) for line in open(events_path)]
    return bool(spawned), row, events


def _of_type(events, event_type):
    return [e for e in events if e.get("event_type") == event_type]


def test_dispatch_records_the_targeted_default(tmp_path, monkeypatch):
    """★★#278 validation 1, through the handler rather than the helper."""
    spawned, row, events = _dispatch(tmp_path, monkeypatch,
                                     lock_base=str(tmp_path / "lb"))
    assert spawned
    assert row["effective_test_scope"] == "targeted"
    assert row["test_scope_source"] == "builtin"
    assert len(row["test_scope_hash"]) == 16

    resolved = _of_type(events, "test_scope_resolved")
    assert len(resolved) == 1
    assert resolved[0]["effective_test_scope"] == "targeted"
    assert resolved[0]["test_scope_hash"] == row["test_scope_hash"]


def test_dispatch_records_an_opted_in_full_suite_and_its_source(tmp_path, monkeypatch):
    """★★#278 validation 2."""
    _, row, events = _dispatch(tmp_path, monkeypatch, lock_base=str(tmp_path / "lb"),
                               scope={"full_suite": True, "full": ["make test"]})
    assert row["effective_test_scope"] == "full_suite"
    assert row["test_scope_source"] == "repo"
    assert _of_type(events, "test_scope_resolved")[0]["effective_test_scope"] == "full_suite"


def test_the_event_stream_carries_no_filesystem_path(tmp_path, monkeypatch):
    """⛔`scope["source"]` is a path under the user's home. The categorical kind
    is what goes out; publishing the path into an economics stream would be a
    privacy regression in the name of telemetry."""
    _, _, events = _dispatch(tmp_path, monkeypatch, lock_base=str(tmp_path / "lb"),
                             scope={"guards": ["ruff check ."]})
    event = _of_type(events, "test_scope_resolved")[0]
    assert event["test_scope_source"] == "repo"
    blob = json.dumps(event)
    assert str(tmp_path) not in blob and "ruff check" not in blob


def test_a_non_test_task_records_no_treatment(tmp_path, monkeypatch):
    """An implement task has no tester scope, and inventing `targeted` for it
    would pollute every cohort built on this column."""
    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    async def _fake_exec(*cmd, **kwargs):
        class _P:
            returncode, pid = 0, 1

            async def wait(self):
                return 0
        return _P()

    wt = tmp_path / "worktrees" / "demo" / "claude"
    wt.mkdir(parents=True)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"port": 0, "worktrees": {"claude": str(wt)}}))
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec", _fake_exec)
    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, pane_map={}, port=0, state_path=str(state),
                     project="demo", watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        q = TaskQueue(db)
        q.enqueue(TaskRequest(task_id="impl-1", task_type="implement",
                              description="go", branch="main", context={}))
        task = q.dequeue(role="implementer")
        asyncio.run(app.state.dispatch_task(task, "implementer"))
        assert q.get_attribution("impl-1")["effective_test_scope"] is None


def test_a_deferred_dispatch_is_observable_and_is_not_runtime(tmp_path, monkeypatch):
    """★★#278 validation 4, with the lock genuinely held by another process.

    ⛔Two assertions, and the second is the one #278 turns on. `started_at` is
      pinned on first write and never overwritten (#204), so if a deferred
      attempt wrote an attribution row it would stamp dispatch time and hand
      the whole lock wait back as provider runtime. The deferred attempt must
      leave no row at all."""
    import subprocess
    import sys
    import textwrap

    base = str(tmp_path / "lb")
    wt = tmp_path / "worktrees" / "demo" / "gemini"
    wt.mkdir(parents=True)
    src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {os.path.join(src, "src")!r})
            from agent_crew.testing_policy import test_stage_lock
            with test_stage_lock({str(wt)!r}, base={base!r}) as ok:
                print("HELD" if ok else "NO", flush=True)
                time.sleep(30)
        """)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "HELD"
        spawned, row, events = _dispatch(tmp_path, monkeypatch, lock_base=base)
    finally:
        holder.kill()
        holder.wait(timeout=10)

    assert spawned is False, "a second test stage ran against a locked worktree"
    assert row is None, "a deferred attempt wrote an attribution row, so started_at is now wrong"

    deferred = _of_type(events, "test_stage_deferred")
    assert len(deferred) == 1
    assert deferred[0]["defer_count"] == 1
    assert deferred[0]["waiting_since"] > 0
    assert not _of_type(events, "task_started"), \
        "a deferred attempt announced itself as started"


def test_the_wait_is_attributed_when_the_task_finally_runs(tmp_path, monkeypatch):
    """The other half: once the lock frees, the accumulated deferral shows up
    as `lock_wait_seconds` on the row that DID run — separable from, and not
    added to, its provider execution time."""
    spawned, row, _ = _dispatch(tmp_path, monkeypatch, lock_base=str(tmp_path / "lb"),
                                defers=2)
    assert spawned
    assert row["lock_defer_count"] == 2
    assert row["lock_wait_seconds"] >= 0
    assert row["effective_test_scope"] == "targeted"


def test_an_uncontended_run_reports_zero_wait_not_unknown(tmp_path, monkeypatch):
    """⛔`0` and NULL mean different things here: measured-and-none versus
    never-measured. A dispatch that took the lock first try has measured it."""
    _, row, _ = _dispatch(tmp_path, monkeypatch, lock_base=str(tmp_path / "lb"))
    assert row["lock_wait_seconds"] == 0.0 and row["lock_defer_count"] == 0
