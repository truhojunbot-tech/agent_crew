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
