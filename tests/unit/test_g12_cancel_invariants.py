"""G12 cancel correctness — the invariants the r1 review of 7747c9f+720ac76 found unguarded.

Three of these exist because a unit test *passed* while the code was broken:

* every dispatcher test in this suite stubs ``create_subprocess_exec`` with a
  stub that ignores ``stdout``/``stderr`` entirely, so re-indenting the spawn
  out of its ``with open(log_path, "ab")`` block — which makes every real
  dispatch raise ``ValueError: I/O operation on closed file`` — was invisible
  to CI.  :func:`_spawn_stub` here honours the real call contract instead.
* nothing asserted that the *real* dispatcher registers its child in
  ``app.state.active_dispatch_processes``, which is the only handle cancel has
  on a worker (I-B).
* I-D (no successor, no attribution for a cancelled lineage) had no test at
  all; it was inferred from the 409 on the late result.
"""

import asyncio
import json
import os
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


# ── the real spawn contract ───────────────────────────────────────────────

class _SpawnContractViolation(AssertionError):
    """Raised when the dispatcher hands the spawn an unusable log handle."""


def _spawn_stub(record, *, returncode=0, on_wait=None):
    """A ``create_subprocess_exec`` stub that uses ``stdout``/``stderr`` the way
    the real one does.

    ``asyncio.create_subprocess_exec`` resolves a file object to an fd via
    ``.fileno()`` and hands that fd to the child.  On a closed handle that is a
    ``ValueError``, not a silent no-op — so the stub must touch it.  A stub that
    only records ``cmd`` cannot tell a working dispatcher from a broken one.
    """
    async def _exec(*cmd, stdout=None, stderr=None, **kwargs):
        for name, handle in (("stdout", stdout), ("stderr", stderr)):
            if handle is None:
                raise _SpawnContractViolation(f"{name} was not passed to the spawn")
            try:
                handle.fileno()              # ValueError on a closed file
                handle.write(b"")            # ...and so is a write
            except ValueError as exc:        # pragma: no cover - the bug's signature
                raise _SpawnContractViolation(
                    f"{name} handle unusable at spawn time: {exc}") from exc
        stdout.write(b"stub-dispatch-output\n")
        record.append(list(cmd))

        class _P:
            pid = 4242

            def __init__(self):
                self.returncode = None

            async def wait(self):
                if on_wait is not None:
                    on_wait(self)
                self.returncode = returncode
                return returncode

        return _P()

    return _exec


def _dispatch_env(tmp_path, monkeypatch, *, agent="claude"):
    wt = tmp_path / "worktrees" / "demo" / agent
    wt.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"port": 8111, "worktrees": {agent: str(wt)}}))
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setenv("AGENT_CREW_BASE", str(tmp_path))
    return str(state)


def _run_one_dispatch(tmp_path, monkeypatch, *, task_id="disp", on_wait=None):
    state_path = _dispatch_env(tmp_path, monkeypatch)
    spawned = []
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec",
                        _spawn_stub(spawned, on_wait=on_wait))
    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, pane_map={}, port=8111, state_path=state_path,
                     project="demo", watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        q = TaskQueue(db)
        q.enqueue(TaskRequest(task_id=task_id, task_type="implement",
                              description="d", branch="main"))
        task = q.dequeue(role="implementer")
        assert task is not None
        asyncio.run(app.state.dispatch_task(task, "implementer"))
    return app, spawned, db


def test_dispatch_spawns_with_an_open_log_handle(tmp_path, monkeypatch):
    """(1) The spawn must happen inside `with open(log_path, "ab")`.

    720ac76 re-indented it one level out, so `log_f` was already closed and
    every dispatch raised "I/O operation on closed file" — caught by the
    dispatcher's broad `except Exception` and reported as
    `dispatcher_exception`.  This asserts the handle is usable *and* that the
    bytes the child writes land in the task's log.
    """
    app, spawned, db = _run_one_dispatch(tmp_path, monkeypatch, task_id="spawn-ok")
    assert spawned, "the dispatcher never reached the spawn"
    logs = []
    for root, _dirs, files in os.walk(tmp_path):
        logs += [os.path.join(root, f) for f in files if f.endswith(".log")]
    written = [p for p in logs if b"stub-dispatch-output" in open(p, "rb").read()]
    assert written, f"child output reached no log file (searched {len(logs)})"
    with sqlite3.connect(db) as c:
        status = c.execute("SELECT status FROM tasks WHERE task_id='spawn-ok'").fetchone()[0]
    assert status != "failed", "dispatch failed despite a clean child exit"


def test_dispatcher_registers_and_unregisters_the_spawned_process(tmp_path, monkeypatch):
    """(3) I-B's precondition: cancel can only signal what the dispatcher registered.

    Observed from inside the child's `wait()`, which is exactly the window a
    cancel would arrive in.
    """
    seen = {}

    def _on_wait(proc):
        registry = app_box["app"].state.active_dispatch_processes
        seen["during"] = registry.get("reg-task")
        seen["pid"] = getattr(seen["during"], "pid", None)

    app_box = {}
    state_path = _dispatch_env(tmp_path, monkeypatch)
    spawned = []
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec",
                        _spawn_stub(spawned, on_wait=_on_wait))
    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, pane_map={}, port=8111, state_path=state_path,
                     project="demo", watchdog_disabled=True, anomaly_disabled=True)
    app_box["app"] = app
    with TestClient(app):
        q = TaskQueue(db)
        q.enqueue(TaskRequest(task_id="reg-task", task_type="implement",
                              description="d", branch="main"))
        task = q.dequeue(role="implementer")
        asyncio.run(app.state.dispatch_task(task, "implementer"))

    assert seen.get("during") is not None, "the spawned process was never registered"
    assert seen["pid"] == 4242
    assert "reg-task" not in app.state.active_dispatch_processes, \
        "the registry leaked the process after the dispatch finished"


# ── I-D: a cancelled lineage mints nothing and is credited with nothing ───

def test_cancel_mints_no_successor_and_attributes_no_commit(tmp_db):
    """(7) I-D: after a cancel there is no fix-/review-/test- successor for the
    lineage, and a commit reported against the cancelled task is not attributed.
    """
    app = create_app(tmp_db, watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/tasks", json={"task_id": "impl-x", "task_type": "implement",
                                    "description": "d", "branch": "main"})
        assert client.delete("/tasks/impl-x").status_code == 200
        late = client.post("/tasks/impl-x/result", json={
            "task_id": "impl-x", "status": "completed", "summary": "done",
            "commit": "cafebabe" * 5, "branch": "main", "pr_number": 99,
        })
    assert late.status_code == 409

    with sqlite3.connect(tmp_db) as c:
        c.row_factory = sqlite3.Row
        ids = [r["task_id"] for r in c.execute("SELECT task_id FROM tasks")]
        row = c.execute("SELECT status, summary FROM tasks WHERE task_id='impl-x'").fetchone()
    assert ids == ["impl-x"], f"cancel minted successors: {sorted(set(ids) - {'impl-x'})}"
    assert not [t for t in ids if t.startswith(("fix-", "review-", "test-"))]
    assert (row["status"], row["summary"]) == ("cancelled", None)

    attribution = TaskQueue(tmp_db).get_attribution("impl-x")
    assert not (attribution or {}).get("commit"), \
        "a commit reported after cancel was attributed to the cancelled task"


def test_cancel_writes_a_parseable_timestamp_into_used_at(tmp_path):
    """(4) `used_at` is an RFC3339 timestamp column; the reason belongs in `used_by`."""
    import datetime as dt

    from agent_crew.cea import store as receipt_store
    from agent_crew.cea.engine import EngineConfig
    from tests.unit.test_sev0_cea_s2c_writer_callsites import WIRED, admitted, task

    db = str(tmp_path / "cancel-ts.db")
    q = TaskQueue(db, cea_config=EngineConfig(mode="test"), cea_providers=dict(WIRED))
    q.enqueue(task("ts-attempt", context=admitted()), ingress="http.tasks")
    assert q.dequeue(role="implementer", agent="claude")
    nonce = q.record_dispatch("ts-attempt", channel="claude_p", agent="claude", target="pid:7")
    q.cancel("ts-attempt")

    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        row = receipt_store.nonce_row(c, nonce)
    assert row["used_by"] == receipt_store.CANCELLED_ATTEMPT_CONSUMER
    # Parses as a timestamp: the whole point of the column.  "cancelled" did not.
    parsed = dt.datetime.strptime(row["used_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert parsed.year >= 2024


# ── (1) a stale-lease expiry is a cancel, not a raw status UPDATE ──────────
#
# `expire_stale` used to do `UPDATE tasks SET status = 'cancelled' WHERE
# task_id IN (...)`.  Everything cancel guarantees was therefore absent on the
# expiry path: the receipt stayed live, the attempt's dispatch nonce stayed
# spendable (so a copied task block still ran), no `task_exec` end event was
# written, and a dispatcher child was neither signalled nor recorded
# (r2 review of 4a49338, finding 1).


class _FakeProc:
    """A dispatcher child we can observe being signalled instead of signalling."""

    def __init__(self, pid=5151, returncode=None):
        self.pid = pid
        self.returncode = returncode

    async def wait(self):                       # pragma: no cover - reap path only
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode


def _cea_queue(db):
    """A CEA-wired queue, so the task under test has a real receipt and nonce."""
    from agent_crew.cea.engine import EngineConfig
    from tests.unit.test_sev0_cea_s2c_writer_callsites import WIRED

    return TaskQueue(db, cea_config=EngineConfig(mode="test"), cea_providers=dict(WIRED))


def _dispatched_stale_task(db, task_id, *, idle_for=1200.0):
    """Enqueue → claim → dispatch a task, then age its lease past any bound."""
    from tests.unit.test_sev0_cea_s2c_writer_callsites import admitted, task as _task

    q = _cea_queue(db)
    q.enqueue(_task(task_id, context=admitted()), ingress="http.tasks")
    assert q.dequeue(role="implementer", agent="claude") is not None
    nonce = q.record_dispatch(task_id, channel="claude_p", agent="claude",
                              target="pid:5151", lease_owner="claude:pending")
    assert nonce, "the fixture needs a real dispatch nonce to prove it gets spent"
    with sqlite3.connect(db) as c:
        c.execute("UPDATE tasks SET last_activity_at = ? WHERE task_id = ?",
                  (time.time() - idle_for, task_id))
    return q, nonce


def _expire(app, db, task_id, *, pid):
    """Create the stale attempt inside the running app, then expire it for real.

    Order matters: startup re-queues in_progress rows, and that re-admission
    SUPERSEDES the receipt — so an attempt built before startup would be
    measuring the fixture's own re-admission and not the expiry.
    """
    try:
        with TestClient(app) as client:
            q, nonce = _dispatched_stale_task(db, task_id)
            app.state.active_dispatch_processes[task_id] = _FakeProc(pid=pid)
            body = client.post("/tasks/expire-stale?older_than=600").json()
            return q, nonce, body
    finally:
        for timer in list(app.state.cancel_kill_timers.values()):
            timer.cancel()
            timer.join(timeout=1.0)
        app.state.cancel_kill_timers.clear()


def test_stale_expiry_goes_through_the_authoritative_cancel(tmp_path, monkeypatch):
    """(1) After an expiry: cancelled, REVOKED, nonce spent with an RFC3339
    `used_at`, a `task_exec` end event naming STALE_LEASE — and the child
    actually signalled."""
    import datetime as dt
    import signal as _signal

    from agent_crew.cea import store as receipt_store

    db = str(tmp_path / "expire.db")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "test")
    monkeypatch.setenv("AGENT_CREW_CANCEL_KILL_GRACE", "0.05")
    signalled = []
    monkeypatch.setattr("agent_crew.server.os.killpg",
                        lambda pid, sig: signalled.append((pid, sig)))
    app = create_app(db, watchdog_disabled=True, anomaly_disabled=True)

    q, nonce, body = _expire(app, db, "stale-1", pid=5151)

    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        receipt_id = c.execute("SELECT receipt_id FROM tasks WHERE task_id='stale-1'"
                               ).fetchone()["receipt_id"]
    assert receipt_id, "the fixture needs a receipt for REVOKED to mean anything"
    assert body["cancelled"] == ["stale-1"]
    # I-B: the child is ours and it was signalled as a group.
    assert body["worker_termination"]["stale-1"] == "sigterm_sent"
    assert signalled[0] == (5151, _signal.SIGTERM)

    state = q.get_exec_state("stale-1")
    assert q.get_task_status("stale-1") == "cancelled"
    # The end event exists at all — the raw UPDATE wrote none — and it says why.
    ends = [e for e in state["events"] if e["event"] == "cancelled"]
    assert ends, f"expiry wrote no task_exec end event: {[e['event'] for e in state['events']]}"
    assert ends[-1]["reason"] == "STALE_LEASE"
    assert state["lease_owner"] is None, "the lease outlived the attempt it bounded"

    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        nrow = receipt_store.nonce_row(c, nonce)
        receipt = receipt_store.current_receipt(c, receipt_id)
    assert nrow["used_at"], "the expired attempt's nonce is still spendable"
    assert nrow["used_by"] == receipt_store.CANCELLED_ATTEMPT_CONSUMER
    dt.datetime.strptime(nrow["used_at"], "%Y-%m-%dT%H:%M:%SZ")      # a timestamp, not a reason
    assert receipt["state"] == "REVOKED", f"receipt left {receipt['state']} after expiry"


def test_stale_expiry_records_an_unsignallable_child_as_orphaned(tmp_path, monkeypatch):
    """(1) A child we cannot signal is not silently forgotten: it is ORPHANED,
    with the pid an operator has to go and find."""
    db = str(tmp_path / "orphan.db")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "test")

    def _gone(pid, sig):
        raise ProcessLookupError(pid)

    monkeypatch.setattr("agent_crew.server.os.killpg", _gone)
    app = create_app(db, watchdog_disabled=True, anomaly_disabled=True)

    q, _nonce, body = _expire(app, db, "stale-2", pid=5252)

    assert body["cancelled"] == ["stale-2"]
    assert body["worker_termination"]["stale-2"] == "process_group_unavailable"
    orphans = [e for e in q.get_exec_state("stale-2")["events"]
               if e["event"] == TaskQueue.WORKER_ORPHANED_EVENT]
    assert orphans, "a child that could not be signalled left no ORPHANED record"
    assert orphans[-1]["state"] == "ORPHANED"
    assert orphans[-1]["pid"] == 5252
    assert orphans[-1]["reason"] == "STALE_LEASE"
    # The task itself is cancelled; ORPHANED describes the process, not the row.
    assert q.get_task_status("stale-2") == "cancelled"


def test_a_task_that_finished_during_the_scan_is_not_expired(tmp_path, monkeypatch):
    """The expiry is a CAS on in_progress, not a blind write.

    The interleaving is the point: the result lands *after* the stale scan has
    already selected the row.  A bulk `UPDATE ... WHERE task_id IN (...)` marks
    that finished task cancelled anyway, which is how a completed attempt gets
    retroactively un-completed.
    """
    db = str(tmp_path / "cas.db")
    q, _nonce = _dispatched_stale_task(db, "done-1")
    real_cancel = TaskQueue.cancel

    def _finish_then_cancel(self, task_id, **kwargs):
        # The worker's result commits between the scan and the cancel.
        with sqlite3.connect(db) as c:
            c.execute("UPDATE tasks SET status = 'completed' WHERE task_id = ?", (task_id,))
        return real_cancel(self, task_id, **kwargs)

    monkeypatch.setattr(TaskQueue, "cancel", _finish_then_cancel)
    assert q.expire_stale(older_than_seconds=600.0) == []
    assert q.get_task_status("done-1") == "completed"


# ── (2) the cancel/spawn handoff ──────────────────────────────────────────
#
# The dispatch commits (`record_dispatch`) several statements before the child
# exists, and the child was registered several statements after it.  A cancel
# landing in that window found `None` in the registry, answered
# `pane_worker_not_killable` — killing nothing — and the dispatcher then
# registered the child and awaited it as a live worker for a task whose
# authorization it had just revoked (r2 review of 4a49338, finding 2).


def _cancel_like_the_endpoint(db, app, task_id, *, via_server=True):
    """The DELETE endpoint's own two steps, in its order: the authoritative
    cancel commits, then I-B stops the worker.

    Called directly rather than through TestClient because this runs *inside*
    the event loop the dispatcher occupies, which a sync test client cannot
    re-enter.  ``via_server=False`` models the other real caller — a cancel from
    another process (`crew task expire-stale`, the CLI), which cannot touch this
    server's in-memory registry at all, so only the DB says the task is over.
    """
    TaskQueue(db).cancel(task_id)
    if not via_server:
        return None
    stop = getattr(app.state, "stop_worker_for_ended_task", None)
    if stop is None:
        # Pre-fix shape: the endpoint looked the process up itself and passed
        # whatever it found (`None`, in this window) straight to termination.
        return app.state.terminate_worker(
            task_id, app.state.active_dispatch_processes.get(task_id))
    return stop(task_id, reason="cancelled_attempt")


def _dispatch_with_cancel_at_spawn(tmp_path, monkeypatch, *, task_id, via_server):
    """Run one real dispatch, injecting a cancel in the handoff window.

    The injection point is the spawn itself: `record_dispatch` has committed and
    the nonce is already in the prompt, and the child is not in the registry
    yet.  That is precisely the window the race lives in.
    """
    state_path = _dispatch_env(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENT_CREW_CANCEL_KILL_GRACE", "0.05")
    signalled = []
    monkeypatch.setattr("agent_crew.server.os.killpg",
                        lambda pid, sig: signalled.append((pid, sig)))
    db = str(tmp_path / "tasks.db")
    spawned = []
    base_exec = _spawn_stub(spawned)
    injected = {}
    box = {}

    async def _exec_then_cancel(*cmd, **kwargs):
        proc = await base_exec(*cmd, **kwargs)
        injected["termination"] = _cancel_like_the_endpoint(
            db, box["app"], task_id, via_server=via_server)
        injected["registry_entry"] = box["app"].state.active_dispatch_processes.get(task_id)
        return proc

    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec", _exec_then_cancel)
    app = create_app(db_path=db, pane_map={}, port=8111, state_path=state_path,
                     project="demo", watchdog_disabled=True, anomaly_disabled=True)
    box["app"] = app
    try:
        with TestClient(app):
            q = TaskQueue(db)
            q.enqueue(TaskRequest(task_id=task_id, task_type="implement",
                                  description="d", branch="main"))
            task = q.dequeue(role="implementer")
            assert task is not None
            asyncio.run(app.state.dispatch_task(task, "implementer"))
    finally:
        for timer in list(app.state.cancel_kill_timers.values()):
            timer.cancel()
            timer.join(timeout=1.0)
        app.state.cancel_kill_timers.clear()
    return app, db, spawned, signalled, injected


def test_a_cancel_in_the_dispatch_handoff_window_stops_the_child(tmp_path, monkeypatch):
    """(2) A cancel between the dispatch commit and the registration finds the
    dispatch, and the child is terminated rather than awaited as live."""
    import signal as _signal

    app, db, spawned, signalled, injected = _dispatch_with_cancel_at_spawn(
        tmp_path, monkeypatch, task_id="race-task", via_server=True)

    assert spawned, "the dispatcher never reached the spawn"
    # The bug's signature: a decided dispatch reported as nothing to kill.
    assert injected["termination"] == "worker_not_spawned_yet", injected["termination"]
    assert injected["registry_entry"] is not None, \
        "the dispatch was invisible to cancel until after the spawn"
    # I-B: the child is stopped, as a group, by the dispatcher's own re-check.
    assert signalled and signalled[0] == (4242, _signal.SIGTERM), signalled

    q = TaskQueue(db)
    state = q.get_exec_state("race-task")
    assert q.get_task_status("race-task") == "cancelled", \
        "the dispatcher overwrote the cancel with its own outcome"
    # Never awaited as live: the pid was never bound, and no dispatcher outcome
    # (timed_out / no_result_submitted / exit_*) was recorded against the task.
    assert state["dispatch_target"] != "pid:4242", \
        "the cancelled child was adopted as the task's live executor"
    assert not [e for e in state["events"] if e["event"] in ("timed_out", "failed")]
    assert "race-task" not in app.state.active_dispatch_processes
    assert not app.state.cancel_kill_timers, "a SIGKILL was left armed after the dispatch"


def test_a_cancel_from_another_process_is_caught_by_the_post_spawn_recheck(tmp_path, monkeypatch):
    """(2) The same window, but the cancel never touched this server's registry
    — `crew task expire-stale` runs in its own process.  Only the re-read of the
    task's status can catch it, so that re-read has to exist."""
    import signal as _signal

    app, db, spawned, signalled, injected = _dispatch_with_cancel_at_spawn(
        tmp_path, monkeypatch, task_id="race-cli", via_server=False)

    assert spawned, "the dispatcher never reached the spawn"
    assert injected["termination"] is None            # no server-side I-B ran
    assert signalled and signalled[0] == (4242, _signal.SIGTERM), \
        "an out-of-process cancel left the child running"
    q = TaskQueue(db)
    assert q.get_task_status("race-cli") == "cancelled"
    assert q.get_exec_state("race-cli")["dispatch_target"] != "pid:4242"
