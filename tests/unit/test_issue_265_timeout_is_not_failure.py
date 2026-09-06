"""#265 — a dispatcher timeout was reported as a failure, then silently revised.

Reported from alpha_engine with six cases in one day. The dispatcher stops
waiting, marks the task `failed`, the worker keeps running and later POSTs its
result, and the row flips to `completed` with nothing announced. A consumer
that read the status when the notification fired saw a false failure — and one
of those tasks had already opened a PR, so re-issuing it would have produced a
duplicate.

Measured across 4,620 attribution rows while confirming the report:

    dispatcher_timeout    n= 27  median= 910s  max=1818s
    no_result_submitted   n= 25  median= 726s  max=1752s
    completed             n=2049 median=  91s  p90= 734s

The timed-out cohort sits exactly at the two walls (900s / 1800s), so these are
wall-clock kills, not crashes — the work was still running.
"""

import json
import time

import pytest

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


@pytest.fixture(autouse=True)
def _no_dispatcher(monkeypatch):
    """⛔The headless dispatcher requeues in_progress orphans at startup, which
    would silently undo the state these tests set up. Off explicitly rather
    than by luck of the ambient environment."""
    monkeypatch.delenv("AGENT_CREW_DISPATCHER", raising=False)


def _in_progress(q, task_id="t-1", task_type="implement"):
    q.enqueue(TaskRequest(task_id=task_id, task_type=task_type,
                          description="do it", branch="main"))
    q.dequeue(role={"implement": "implementer", "review": "reviewer"}[task_type])
    return task_id


# ── 1. a timeout is not a failure ─────────────────────────────────────


def _dispatch_outcome(tmp_path, monkeypatch, *, behaviour):
    """Drive the REAL dispatch path and return the task row it ends with.

    ⛔Goes through `_dispatch_task`, not through the terminal-marking helper.
      A first version of these tests called that helper with the status passed
      in by hand — which asserts the helper works and says nothing about which
      status the DISPATCHER chooses. Reverting the call site was invisible to
      it; this shape fails when the dispatcher calls it wrongly.
    """
    import asyncio

    from fastapi.testclient import TestClient

    from agent_crew import server as sv
    from agent_crew.server import create_app

    wt = tmp_path / "claude"
    wt.mkdir(exist_ok=True)
    (wt / ".git").mkdir(exist_ok=True)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"worktrees": {"claude": str(wt)}}))
    db = str(tmp_path / "t.db")

    async def _fake_exec(*cmd, **kwargs):
        class _P:
            returncode = 0 if behaviour != "exit_1" else 1
            pid = 4242

            async def communicate(self):
                return (b"", b"")

            async def wait(self):
                if behaviour == "hang":
                    await asyncio.sleep(30)     # outlives the timeout below
                return self.returncode

            def kill(self):
                # The dispatcher kills the process group and then the child on
                # timeout; a fake without this lands in the generic exception
                # handler and the task is marked `dispatcher_exception`, which
                # is not the path under test.
                pass

            def terminate(self):
                pass

        return _P()

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(sv, "_dispatch_timeout_for_role", lambda role: 0.05)
    monkeypatch.setattr(sv.os, "killpg", lambda *a, **k: None)

    app = create_app(db_path=db, pane_map={}, port=0, state_path=str(state),
                     project="p", watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        q = TaskQueue(db)
        q.enqueue(TaskRequest(task_id="t-1", task_type="implement",
                              description="do it", branch="main"))
        task = q.dequeue(role="implementer")
        assert task is not None
        asyncio.run(app.state.dispatch_task(task, "implementer"))
        return next(t for t in q.list_tasks() if t.task_id == "t-1")


def test_a_dispatcher_timeout_ends_the_task_as_timed_out(tmp_path, monkeypatch):
    """★The ask: "failed" and "we stopped waiting" must be distinguishable,
    and the DISPATCHER has to be the one making that distinction."""
    task = _dispatch_outcome(tmp_path, monkeypatch, behaviour="hang")

    assert task.status == "timed_out", "a timeout is still reported as a failure"
    assert task.error_info["reason"] == "dispatcher_timeout"
    assert task.error_info["final"] is False, (
        "a timeout must say it is not a final verdict — that is the whole "
        "distinction the consumer needs"
    )


def test_a_clean_exit_without_a_result_is_also_timed_out(tmp_path, monkeypatch):
    """The process is gone, but the POST can still be in flight. "We did not
    observe a result" is not "the work failed"."""
    task = _dispatch_outcome(tmp_path, monkeypatch, behaviour="clean")

    assert task.status == "timed_out"
    assert task.error_info["reason"] == "no_result_submitted"


def test_a_real_failure_is_still_failed(tmp_path, monkeypatch):
    """⛔The distinction cuts both ways. A non-zero exit is a failure and must
    not be softened into "we do not know"."""
    task = _dispatch_outcome(tmp_path, monkeypatch, behaviour="exit_1")

    assert task.status == "failed"
    assert task.error_info["reason"] == "exit_1"
    assert task.error_info["final"] is True


def test_the_reason_survives_on_a_timed_out_task(tmp_db):
    """⛔`error_info` used to be persisted only for `failed`, so a timed-out
    task carried no machine-readable cause — the reporter could not tell why."""
    q = TaskQueue(tmp_db)
    tid = _in_progress(q)

    q.submit_result(tid, TaskResult(task_id=tid, status="timed_out",
                                    summary="dispatcher_timeout",
                                    error_info={"reason": "dispatcher_timeout"}))

    task = next(t for t in q.list_tasks() if t.task_id == tid)
    assert task.error_info == {"reason": "dispatcher_timeout"}


def test_timed_out_is_a_valid_result_status():
    """It is already in the protocol and already terminal for the watcher —
    this reuses that vocabulary rather than inventing a new one."""
    from agent_crew.protocol import _VALID_RESULT_STATUSES
    from agent_crew.watch import _TERMINAL_TASK_STATUSES

    assert "timed_out" in _VALID_RESULT_STATUSES
    assert "timed_out" in _TERMINAL_TASK_STATUSES


# ── 2. the late revision is announced ─────────────────────────────────


def test_a_late_result_is_recorded_as_an_event(tmp_db, tmp_path, monkeypatch):
    """★★The silent flip. A consumer that read `timed_out` at notification time
    must be able to learn the verdict was revised."""
    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    db = str(tmp_path / "t.db")
    q = TaskQueue(db)
    tid = _in_progress(q)
    q.submit_result(tid, TaskResult(task_id=tid, status="timed_out",
                                    summary="dispatcher_timeout",
                                    error_info={"reason": "dispatcher_timeout"}))

    app = create_app(db_path=db, pane_map={}, port=0, watchdog_disabled=True,
                     anomaly_disabled=True)
    with TestClient(app) as c:
        r = c.post(f"/tasks/{tid}/result",
                   json={"task_id": tid, "status": "completed",
                         "summary": "actually finished", "verdict": None,
                         "findings": [], "pr_number": None})
        assert r.status_code == 200

    events = [json.loads(l) for l in open(tmp_path / "context_events.jsonl")]
    late = [e for e in events if e["event_type"] == "task_result_late"]
    assert late, "the revision was silent — exactly the reported defect"
    assert late[0]["previous_status"] == "timed_out"
    assert late[0]["new_status"] == "completed"


def test_an_ordinary_result_is_not_announced_as_late(tmp_db, tmp_path):
    """⛔Only a REVISION is news. Announcing every result would bury it."""
    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    db = str(tmp_path / "t.db")
    q = TaskQueue(db)
    tid = _in_progress(q)

    app = create_app(db_path=db, pane_map={}, port=0, watchdog_disabled=True,
                     anomaly_disabled=True)
    with TestClient(app) as c:
        c.post(f"/tasks/{tid}/result",
               json={"task_id": tid, "status": "completed", "summary": "done",
                     "verdict": None, "findings": [], "pr_number": None})

    path = tmp_path / "context_events.jsonl"
    events = [json.loads(l) for l in open(path)] if path.exists() else []
    assert not [e for e in events if e["event_type"] == "task_result_late"]


def test_status_changed_at_moves_when_the_verdict_is_revised(tmp_db):
    """The field a polling consumer can use without reading the event stream."""
    q = TaskQueue(tmp_db)
    tid = _in_progress(q)

    q.submit_result(tid, TaskResult(task_id=tid, status="timed_out",
                                    summary="dispatcher_timeout",
                                    error_info={"reason": "dispatcher_timeout"}))
    first = next(t for t in q.list_tasks() if t.task_id == tid).status_changed_at
    time.sleep(0.01)
    q.submit_result(tid, TaskResult(task_id=tid, status="completed", summary="done"))
    second = next(t for t in q.list_tasks() if t.task_id == tid).status_changed_at

    assert first > 0 and second > first


def test_the_late_result_is_still_accepted_and_durable(tmp_db, tmp_path):
    """⛔Announcing the revision must not mean rejecting it. The work was real;
    the record has to reflect it."""
    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    db = str(tmp_path / "t.db")
    q = TaskQueue(db)
    tid = _in_progress(q)
    q.submit_result(tid, TaskResult(task_id=tid, status="timed_out",
                                    summary="dispatcher_timeout",
                                    error_info={"reason": "dispatcher_timeout"}))

    app = create_app(db_path=db, pane_map={}, port=0, watchdog_disabled=True,
                     anomaly_disabled=True)
    with TestClient(app) as c:
        c.post(f"/tasks/{tid}/result",
               json={"task_id": tid, "status": "completed",
                     "summary": "Root-caused the starvation", "verdict": None,
                     "findings": [], "pr_number": 5517})
        got = c.get(f"/tasks/{tid}").json()

    assert got["status"] == "completed"
    assert got["pr_number"] == 5517
    assert "Root-caused" in got["summary"]
