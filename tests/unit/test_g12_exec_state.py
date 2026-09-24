"""G12 / D6: per-task execution state — claim, dispatch, lease, heartbeat, build.

SEV-0 alfred#51 c5777790815 §2. The tasks table had 17 columns and could not
say who claimed a task, where it went, whether it was alive, or which build
handed it out; `push_at` was 0 on 6801/6806 preserved rows (RECONCILIATION.md
F9), because only the tmux path wrote it.
"""

import asyncio
import dataclasses
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_crew import provenance
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import EXEC_STATE_COLUMNS, TaskQueue
from agent_crew.server import create_app

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pre_g12_tasks.db"
NEW_COLUMNS = [c for c in EXEC_STATE_COLUMNS if c != "push_at"]


def _columns(db):
    with sqlite3.connect(db) as c:
        return [r[1] for r in c.execute("PRAGMA table_info(tasks)")]


def _rows(db, cols):
    with sqlite3.connect(db) as c:
        return c.execute(f"SELECT {', '.join(cols)} FROM tasks ORDER BY task_id").fetchall()


def _task(task_id, task_type="implement", **kw):
    return TaskRequest(task_id=task_id, task_type=task_type, description="d",
                       branch="main", **kw)


# ---------------------------------------------------------------------------
# Migration — on a copy of a DB written by the pre-G12 code (5efea31).
# ---------------------------------------------------------------------------

@pytest.fixture
def legacy_db(tmp_path):
    db = tmp_path / "tasks.db"
    shutil.copy(FIXTURE, db)
    return str(db)


def test_fixture_is_the_pre_g12_shape(legacy_db):
    """The premise: 17 columns, as in every preserved SEV-0 DB."""
    cols = _columns(legacy_db)
    assert len(cols) == 17
    assert not set(NEW_COLUMNS) & set(cols)


def test_migration_adds_columns_and_history_without_touching_old_data(legacy_db):
    old_cols = _columns(legacy_db)
    before = _rows(legacy_db, old_cols)

    TaskQueue(legacy_db)

    cols = _columns(legacy_db)
    assert cols[:17] == old_cols                       # additive: order and names kept
    assert cols[17:] == NEW_COLUMNS
    assert _rows(legacy_db, old_cols) == before        # every old value unchanged
    # Old rows carry no invented claim: NULL, not 0 / ''.
    assert all(v is None for row in _rows(legacy_db, NEW_COLUMNS) for v in row)
    with sqlite3.connect(legacy_db) as c:
        assert c.execute("SELECT COUNT(*) FROM task_exec_events").fetchone()[0] == 0


def test_migration_is_idempotent(legacy_db):
    TaskQueue(legacy_db)
    once = (_columns(legacy_db), _rows(legacy_db, _columns(legacy_db)))
    TaskQueue(legacy_db)
    assert (_columns(legacy_db), _rows(legacy_db, _columns(legacy_db))) == once


def test_get_task_reports_legacy_rows_as_unrecorded(legacy_db):
    app = create_app(legacy_db, watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        done = client.get("/tasks/legacy-done").json()
        running = client.get("/tasks/legacy-running").json()
    # Existing fields are unchanged; `execution` is the only addition.
    assert set(done) == {f.name for f in dataclasses.fields(TaskRequest)} | {"execution"}
    assert (done["task_id"], done["task_type"], done["priority"], done["context"]["k"],
            done["project"]) == ("legacy-done", "implement", 2, "v", "legacy")
    assert all(done["execution"][c] is None for c in NEW_COLUMNS)
    assert done["execution"]["events"] == []
    assert running["execution"]["push_at"] == 1234.5   # pre-existing value surfaces


def test_migrated_legacy_rows_keep_working(legacy_db):
    q = TaskQueue(legacy_db)
    task = q.dequeue(role="tester")
    assert task.task_id == "legacy-pending"
    state = q.get_exec_state("legacy-pending")
    assert state["claimed_by_role"] == "tester"
    assert [e["event"] for e in state["events"]] == ["claimed"]


# ---------------------------------------------------------------------------
# Claim / lease / heartbeat in the queue.
# ---------------------------------------------------------------------------

def test_claim_records_role_path_and_running_build(tmp_db):
    q = TaskQueue(tmp_db)
    q.enqueue(_task("c1"))
    q.dequeue(role="implementer", claimed_via="dispatcher")

    state = q.get_exec_state("c1")
    build = provenance.build()
    assert state["claimed_at"] is not None
    assert state["claimed_by_role"] == "implementer"
    assert state["claimed_via"] == "dispatcher"
    assert state["claimed_by_agent"] is None            # not known at this claim
    assert state["claim_build_commit"] == (build["commit"] or None)
    assert state["claim_code_fingerprint"] == (build["code_fingerprint"] or None)
    [event] = state["events"]
    assert event["event"] == "claimed" and event["via"] == "dispatcher"


def test_agent_and_discuss_claims_record_the_agent(tmp_db):
    q = TaskQueue(tmp_db)
    q.enqueue(_task("c2", context={"agent_override": "codex"}))
    q.enqueue(_task("d1", task_type="discuss", context={"agent": "gemini"}))
    q.dequeue(agent="codex", role="implementer", claimed_via="mcp")
    q.dequeue_discuss_for_agent("gemini", claimed_via="tmux_push")
    assert q.get_exec_state("c2")["claimed_by_agent"] == "codex"
    d = q.get_exec_state("d1")
    assert (d["claimed_by_role"], d["claimed_by_agent"], d["claimed_via"]) == (
        "discuss", "gemini", "tmux_push")


def test_requeue_and_reclaim_keep_the_whole_history(tmp_db):
    q = TaskQueue(tmp_db)
    q.enqueue(_task("h1"))
    q.dequeue(role="implementer", claimed_via="dispatcher")
    q.record_dispatch("h1", channel="claude_p", agent="claude", target="pid:7",
                      lease_owner="claude:pid:7", lease_seconds=60, ts=100.0)
    assert q.get_exec_state("h1")["lease_expires_at"] == 160.0
    q.requeue("h1")
    state = q.get_exec_state("h1")
    assert state["lease_owner"] is None and state["lease_expires_at"] is None
    q.dequeue(role="implementer", claimed_via="dispatcher")
    q.record_dispatch("h1", channel="claude_p", agent="claude", target="pid:8",
                      lease_owner="claude:pid:8", lease_seconds=60, ts=200.0)
    q.submit_result("h1", TaskResult(task_id="h1", status="completed", summary="ok",
                                     verdict=None, findings=[], pr_number=None))

    state = q.get_exec_state("h1")
    assert [e["event"] for e in state["events"]] == [
        "claimed", "dispatched", "requeued", "claimed", "dispatched", "result"]
    assert [e["target"] for e in state["events"] if e["event"] == "dispatched"] == [
        "pid:7", "pid:8"]
    assert state["dispatch_attempt"] == 2
    assert state["result_posted_at"] is not None
    assert state["lease_owner"] is None                # the result ends the lease
    assert state["events"][-1]["status"] == "completed"


def test_force_fail_ends_the_lease_without_claiming_a_result(tmp_db):
    q = TaskQueue(tmp_db)
    q.enqueue(_task("ff"))
    q.dequeue(role="implementer", claimed_via="tmux_push")
    q.record_dispatch("ff", channel="tmux_pane", target="%1", lease_owner="pane:%1")
    q.force_fail("ff", "watchdog timeout", error_info={"reason": "watchdog_timeout"})
    state = q.get_exec_state("ff")
    assert state["lease_owner"] is None
    assert state["result_posted_at"] is None
    last = state["events"][-1]
    assert (last["event"], last["reason"], last["from_status"]) == (
        "force_failed", "watchdog_timeout", "in_progress")


def test_cancel_revokes_dispatched_attempt_and_refuses_its_nonce(tmp_path, monkeypatch):
    """I-A: cancellation invalidates the receipt and every credential in its attempt."""
    from agent_crew.cea import store as receipt_store
    from agent_crew.cea.engine import EngineConfig
    from tests.unit.test_sev0_cea_s2c_writer_callsites import WIRED, admitted, task

    db = str(tmp_path / "cancel.db")
    q = TaskQueue(db, cea_config=EngineConfig(mode="test"), cea_providers=dict(WIRED))
    q.enqueue(task("cancel-attempt", context=admitted()), ingress="http.tasks")
    assert q.dequeue(role="implementer", agent="claude")
    nonce = q.record_dispatch("cancel-attempt", channel="claude_p", agent="claude", target="pid:7")
    assert nonce
    q.cancel("cancel-attempt")

    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        rid = c.execute("SELECT receipt_id FROM tasks WHERE task_id='cancel-attempt'").fetchone()["receipt_id"]
        assert receipt_store.current_receipt(c, rid)["state"] == "REVOKED"
        assert receipt_store.nonce_row(c, nonce)["used_by"] == "cancelled_attempt"
        assert c.execute("SELECT COUNT(*) FROM authorization_receipts WHERE receipt_id=?", (rid,)).fetchone()[0] >= 2
    start = q.start_execution("cancel-attempt", nonce, presenter="claude")
    assert start["go"] is False and start["reason"].startswith("CANCELLED_ATTEMPT:")


def test_cancelled_result_is_rejected_without_row_or_artifact_mutation(tmp_db):
    """I-C/D: a late result cannot revive a cancel or create completion evidence."""
    app = create_app(tmp_db, watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/tasks", json={"task_id": "late", "task_type": "implement", "description": "d", "branch": "main"})
        assert client.delete("/tasks/late").status_code == 200
        response = client.post("/tasks/late/result", json={"task_id": "late", "status": "completed", "summary": "late", "commit": "deadbeef"})
    assert response.status_code == 409
    assert "LATE_RESULT_REJECTED" in response.text
    with sqlite3.connect(tmp_db) as c:
        row = c.execute("SELECT status, summary FROM tasks WHERE task_id='late'").fetchone()
    assert row == ("cancelled", None)


class _FakeProc:
    """A worker whose exit is under the test's control, not the clock's."""

    def __init__(self, pid=4242):
        self.pid = pid
        self.returncode = None

    def exit(self, code=0):
        self.returncode = code


@pytest.fixture
def cancel_app(tmp_db, monkeypatch):
    """An app whose cancel-path SIGKILL escalation cannot outlive the test.

    ⛔The original version of these tests armed a bare `threading.Timer(2.0,
      ...)` around a REAL `os.killpg`, against a fake proc whose `returncode`
      never flipped. The timer fired after monkeypatch teardown, so the kill
      went out for real — at a pid the test never owned and the kernel may
      since have recycled. Here `os.killpg` is recorded rather than sent, the
      grace period is small enough to observe, and anything still armed is
      defused before the `monkeypatch` fixture (torn down after this one)
      restores the real syscall.
    """
    monkeypatch.setenv("AGENT_CREW_CANCEL_KILL_GRACE", "0.05")
    app = create_app(tmp_db, watchdog_disabled=True, anomaly_disabled=True)
    app.state.killpg_calls = []
    monkeypatch.setattr("agent_crew.server.os.killpg",
                        lambda pid, sig: app.state.killpg_calls.append((pid, sig)))
    try:
        yield app
    finally:
        for timer in list(app.state.cancel_kill_timers.values()):
            timer.cancel()
            timer.join(timeout=1.0)
        app.state.cancel_kill_timers.clear()


def _enqueue_and_cancel(app, task_id="worker"):
    with TestClient(app) as client:
        client.post("/tasks", json={"task_id": task_id, "task_type": "implement",
                                    "description": "d", "branch": "main"})
        return client.delete(f"/tasks/{task_id}")


def test_cancel_sends_sigterm_to_the_dispatched_process_group(cancel_app):
    """I-B: dispatcher-owned workers are stopped as a process group on cancel."""
    import signal as _signal

    proc = _FakeProc()
    cancel_app.state.active_dispatch_processes["worker"] = proc
    response = _enqueue_and_cancel(cancel_app)
    assert response.json()["worker_termination"] == "sigterm_sent"
    assert cancel_app.state.killpg_calls[0] == (4242, _signal.SIGTERM)
    assert "worker" in cancel_app.state.cancel_kill_timers, \
        "the SIGKILL escalation must be addressable so it can be defused"


def test_pending_sigkill_is_defused_when_the_worker_honours_sigterm(cancel_app):
    """The escalation must be a no-op once the process is actually gone."""
    import signal as _signal

    proc = _FakeProc(pid=4343)
    cancel_app.state.active_dispatch_processes["worker"] = proc
    _enqueue_and_cancel(cancel_app)
    proc.exit(0)                                  # worker obeys the SIGTERM
    timer = cancel_app.state.cancel_kill_timers["worker"]
    timer.join(timeout=2.0)
    assert not timer.is_alive()
    assert [sig for _pid, sig in cancel_app.state.killpg_calls] == [_signal.SIGTERM]
    assert "worker" not in cancel_app.state.cancel_kill_timers


def test_sigkill_escalates_only_while_the_worker_is_still_alive(cancel_app):
    """A worker that ignores SIGTERM gets SIGKILL — at its own pid, once."""
    import signal as _signal

    cancel_app.state.active_dispatch_processes["worker"] = _FakeProc(pid=4444)
    _enqueue_and_cancel(cancel_app)
    cancel_app.state.cancel_kill_timers["worker"].join(timeout=2.0)
    assert cancel_app.state.killpg_calls == [(4444, _signal.SIGTERM), (4444, _signal.SIGKILL)]
    assert not cancel_app.state.cancel_kill_timers


def test_cancel_of_a_pane_worker_says_so_rather_than_not_dispatched(cancel_app):
    """A pane worker has no PID we own; `not_dispatched` claimed the opposite —
    the task WAS dispatched, it just is not ours to kill."""
    response = _enqueue_and_cancel(cancel_app, task_id="pane")
    assert response.json()["worker_termination"] == "pane_worker_not_killable"
    assert cancel_app.state.killpg_calls == []


def test_cancel_commits_before_it_terminates_and_kills_nothing_on_failure(cancel_app, monkeypatch):
    """I-A precedes I-B. If the authoritative cancel does not commit, the caller
    gets a 5xx and the worker is left running — a killed-but-still-authorized
    worker is the worse of the two failures."""
    order = []
    proc = _FakeProc(pid=4545)
    cancel_app.state.active_dispatch_processes["worker"] = proc
    real_terminate = cancel_app.state.terminate_worker
    monkeypatch.setattr(cancel_app.state, "terminate_worker",
                        lambda *a, **kw: (order.append("terminate"), real_terminate(*a, **kw))[1],
                        raising=False)

    with TestClient(cancel_app) as client:
        client.post("/tasks", json={"task_id": "worker", "task_type": "implement",
                                    "description": "d", "branch": "main"})
        from agent_crew.queue import TaskQueue as _TQ
        real_cancel = _TQ.cancel

        def _cancel(self, task_id):
            order.append("commit")
            return real_cancel(self, task_id)

        monkeypatch.setattr(_TQ, "cancel", _cancel)
        assert client.delete("/tasks/worker").status_code == 200
        assert order == ["commit", "terminate"] or order == ["commit"], order

        # Now make the commit fail: nothing may be signalled.
        monkeypatch.setattr(_TQ, "cancel", lambda self, task_id: (_ for _ in ()).throw(RuntimeError("db down")))
        before = list(cancel_app.state.killpg_calls)
        failed = client.delete("/tasks/worker")
    assert failed.status_code == 500
    assert cancel_app.state.killpg_calls == before, "a failed cancel must signal nothing"


def test_heartbeat_only_touches_running_tasks(tmp_db):
    q = TaskQueue(tmp_db)
    q.enqueue(_task("hb"))
    q.record_heartbeat("hb", source="pane_busy", ts=5.0)       # still pending
    assert q.get_exec_state("hb")["last_heartbeat_at"] is None
    q.dequeue(role="implementer")
    q.record_heartbeat("hb", source="pane_busy", ts=6.0)
    state = q.get_exec_state("hb")
    assert (state["last_heartbeat_at"], state["last_heartbeat_source"]) == (6.0, "pane_busy")
    assert "heartbeat" not in [e["event"] for e in state["events"]]


# ---------------------------------------------------------------------------
# Population on the delivery paths.
# ---------------------------------------------------------------------------

class RecordingPush:
    def __init__(self):
        self.calls = []

    def __call__(self, target, message):
        self.calls.append((target, message))


def test_push_path_records_claim_push_and_dispatch(tmp_db, tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"project": "p", "pane_ids": ["%101"]}))
    push = RecordingPush()
    app = create_app(tmp_db, pane_map={"implementer": "%101"}, state_path=str(state_file),
                     port=8100, push_fn=push, watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        client.post("/tasks", json={"task_id": "p1", "task_type": "implement",
                                    "description": "w", "branch": "main", "priority": 3,
                                    "context": {}, "project": "p"})
        execution = client.get("/tasks/p1").json()["execution"]

    assert len(push.calls) == 1
    assert execution["claimed_via"] == "tmux_push"
    assert execution["push_at"] > 0                    # the column #152 already had
    assert execution["dispatch_channel"] == "tmux_pane"
    assert execution["dispatch_target"] == "%101"
    assert execution["lease_owner"] == "pane:%101"
    assert execution["lease_expires_at"] is None       # no deadline on a pane task
    assert [e["event"] for e in execution["events"]] == ["claimed", "pushed", "dispatched"]


def test_http_poll_records_an_api_dispatch(tmp_db):
    app = create_app(tmp_db, watchdog_disabled=True, anomaly_disabled=True)
    TaskQueue(tmp_db).enqueue(_task("api1", context={"agent_override": "claude"}))
    with TestClient(app) as client:
        assert client.get("/tasks/next", params={"agent": "claude",
                                                 "role": "implementer"}).json()
    state = TaskQueue(tmp_db).get_exec_state("api1")
    assert (state["claimed_via"], state["dispatch_channel"], state["dispatch_target"]) == (
        "http_poll", "api", "http_poll:claude")


def test_dispatcher_path_records_pid_lease_and_heartbeat(tmp_path, monkeypatch):
    wt = tmp_path / "claude"
    wt.mkdir()
    (wt / ".git").mkdir()
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"worktrees": {"claude": str(wt)}}))
    db = str(tmp_path / "t.db")

    class _Proc:
        pid = 4242
        returncode = None

        async def wait(self):
            await asyncio.sleep(0.05)                  # long enough for a heartbeat
            self.returncode = 0
            return 0

        async def communicate(self):
            return b"", b""

    async def _fake_exec(*cmd, **kwargs):
        return _Proc()

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec", _fake_exec)
    monkeypatch.setattr("agent_crew.server._HEARTBEAT_INTERVAL_S", 0.01)
    monkeypatch.setattr("agent_crew.server._dispatch_timeout_for_role", lambda _r: 600.0)

    app = create_app(db_path=db, pane_map={}, port=8199, state_path=str(state_file),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        q = TaskQueue(db)
        q.enqueue(_task("disp1"))
        task = q.dequeue(role="implementer", claimed_via="dispatcher")
        asyncio.run(app.state.dispatch_task(task, "implementer"))

    state = TaskQueue(db).get_exec_state("disp1")
    assert state["claimed_via"] == "dispatcher"
    assert state["dispatch_channel"] == "claude_p"
    assert state["dispatch_agent"] == "claude"
    assert state["dispatch_target"] == "pid:4242"
    dispatched = next(e for e in state["events"] if e["event"] == "dispatched")
    assert dispatched["lease_owner"] == "claude:pid:4242"
    assert dispatched["lease_expires_at"] == pytest.approx(dispatched["at"] + 600.0)
    assert state["last_heartbeat_source"] == "process_alive"
    assert state["last_heartbeat_at"] >= state["dispatched_at"]


def test_every_claim_site_is_tagged():
    """No untagged claim path in server or MCP."""
    import inspect
    import agent_crew.mcp_server as mcp_server
    import agent_crew.server as server
    src = inspect.getsource(server) + inspect.getsource(mcp_server)
    for call in (".dequeue(", ".dequeue_discuss_for_agent("):
        sites = [line for line in src.splitlines() if call in line and "def " not in line]
        assert sites and all("claimed_via=" in line for line in sites), sites


# ---------------------------------------------------------------------------
# No behaviour change in dispatch decisions.
# ---------------------------------------------------------------------------

def _script(q):
    """A fixed mix of priorities, overrides, discuss and requeues; returns what
    every dequeue chose and the final status of every task."""
    for tid, tt, pr, ctx in [
        ("a", "implement", 3, {}), ("b", "implement", 1, {}),
        ("c", "implement", 2, {"agent_override": "codex"}), ("d", "review", 2, {}),
        ("e", "test", 1, {}), ("f", "discuss", 3, {"agent": "gemini"}),
    ]:
        q.enqueue(TaskRequest(task_id=tid, task_type=tt, description="d", branch="main",
                              priority=pr, context=ctx))
    picks = []

    def pick(t):
        picks.append(t.task_id if t else None)
        return t

    first = pick(q.dequeue(role="implementer", claimed_via="dispatcher"))
    q.requeue(first.task_id)
    pick(q.dequeue(role="implementer", claimed_via="tmux_push"))
    pick(q.dequeue(agent="codex", role="implementer", claimed_via="mcp"))
    pick(q.dequeue(role="reviewer", claimed_via="dispatcher"))
    pick(q.dequeue_discuss_for_agent("gemini", claimed_via="dispatcher"))
    pick(q.dequeue(role="tester"))
    pick(q.dequeue(role="implementer"))
    pick(q.dequeue(role="implementer"))
    with sqlite3.connect(q._db_path) as c:
        final = c.execute("SELECT task_id, status FROM tasks ORDER BY task_id").fetchall()
    return picks, final


def test_dispatch_decisions_are_identical_with_instrumentation_disabled(tmp_path, monkeypatch):
    instrumented = _script(TaskQueue(str(tmp_path / "on.db")))

    monkeypatch.setattr(TaskQueue, "_record_claim_on", lambda *a, **k: None)
    monkeypatch.setattr(TaskQueue, "_record_end_on", lambda *a, **k: None)
    monkeypatch.setattr(TaskQueue, "_append_exec_event_on", staticmethod(lambda *a, **k: None))
    bare = _script(TaskQueue(str(tmp_path / "off.db")))

    assert instrumented == bare
    assert instrumented[0] == ["b", "b", "c", "d", "f", "e", "a", None]


def test_a_failing_recorder_never_blocks_a_claim(tmp_db):
    """Drop the history table under a live queue: claims, dispatch records,
    requeues and results all still commit."""
    q = TaskQueue(tmp_db)
    q.enqueue(_task("x"))
    with sqlite3.connect(tmp_db) as c:
        c.execute("DROP TABLE task_exec_events")

    task = q.dequeue(role="implementer", claimed_via="dispatcher")
    q.record_dispatch("x", channel="claude_p", target="pid:1")
    q.requeue("x")
    again = q.dequeue(role="implementer")
    q.submit_result("x", TaskResult(task_id="x", status="completed", summary="ok"))

    assert task.task_id == "x" and again.task_id == "x"
    with sqlite3.connect(tmp_db) as c:
        assert c.execute("SELECT status FROM tasks WHERE task_id='x'").fetchone()[0] == "completed"
