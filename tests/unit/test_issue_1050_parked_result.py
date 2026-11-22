"""#1050: a result for a row that never ran is refused, and `crew run` does not
send one.

On 11-21 (alpha_engine, 630fdccf) ``impl-48142056`` was parked
``waiting_pool`` by the #1033 gate. An hour later the ``crew run`` wrapper hit
its deadline and POSTed ``failed:wrapper_timeout``; the server accepted it for
a row that was never dispatched, marked it failed and spent an auto-retry.
"""

import asyncio
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.server import create_app
from agent_crew.queue import ParkedResultRejected, TaskQueue
from tests.dispatch_provenance import record_dispatch_provenance
from tests.unit.test_issue_1033_pool_hold import (  # noqa: F401 (notices: fixture)
    CLAUDE_IMPL, _app, _body, _events, _impl, _signal, _wait, notices)

HDR = {"X-Agent-Crew-Project": "agent_crew"}


def _wrapper_timeout(task_id):
    return {"task_id": task_id, "status": "failed",
            "summary": "watchdog timeout: crew run wrapper exited",
            "error_info": {"kind": "timeout", "reason": "wrapper_timeout"},
            "findings": [], "verdict": None, "pr_number": None}


def _result(task_id, status, reason):
    """A result body for ``task_id``; ``reason=None`` sends no error_info."""
    body = {"task_id": task_id, "status": status, "summary": f"{status} report",
            "findings": [], "verdict": None, "pr_number": None,
            "error_info": None if reason is None else {"kind": "x", "reason": reason}}
    if status == "completed":
        body.update(branch="agent/x", commit="a" * 40, pr_number=1)
    return body


def _own_events(queue, task_id):
    """Exec events minus the park's async coordinator notice, which can land
    after any snapshot and is not written by the result path."""
    return [e["event"] for e in queue.get_exec_state(task_id)["events"]
            if e["event"] != "coordinator_notice"]


def _lineage(queue, task_id):
    return [t.task_id for t in queue.list_tasks() if t.task_id != task_id]


@pytest.mark.parametrize("reason", ["wrapper_timeout", "other", None])
@pytest.mark.parametrize("status", ["completed", "failed", "needs_human"])
def test_waiting_pool_refuses_every_result(tmp_path, monkeypatch, notices, status, reason):
    """A waiting_pool row refuses every result, whatever the status or reason."""
    _signal(tmp_path, monkeypatch, "hold")
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    task_id = _impl(queue)
    body = _result(task_id, status, reason)
    with TestClient(app) as client:
        assert _wait(lambda: queue.get_task_status(task_id) == "waiting_pool")
        before = _own_events(queue, task_id)
        with pytest.raises(ParkedResultRejected):
            queue.submit_result(task_id, TaskResult(**body))
        resp = client.post(f"/tasks/{task_id}/result", json=body, headers=HDR)
        assert resp.status_code == 409, resp.text
        assert resp.json()["prior_status"] == "waiting_pool"
        assert queue.get_task_status(task_id) == "waiting_pool"
        assert queue.get_result(task_id) is None
        assert _own_events(queue, task_id) == before
        assert _events(queue, task_id, "result") == []
        assert _lineage(queue, task_id) == []
        assert dispatched == []


def test_parked_row_wrapper_timeout_is_409_and_row_resumes(tmp_path, monkeypatch, notices):
    """The 8101 incident end to end (alpha_engine ``impl-48142056``, 11-21):
    parked by the #1033 gate, then a ``crew run`` wrapper timeout POST."""
    path = _signal(tmp_path, monkeypatch, "hold")
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    task_id = _impl(queue, task_id="impl-48142056")
    with TestClient(app) as client:
        assert _wait(lambda: queue.get_task_status(task_id) == "waiting_pool")
        before = _own_events(queue, task_id)
        resp = client.post(f"/tasks/{task_id}/result", json=_wrapper_timeout(task_id),
                           headers=HDR)
        assert resp.status_code == 409, resp.text
        assert resp.json()["prior_status"] == "waiting_pool"
        assert resp.json()["accepted"] is False
        # No state change, no exec event, no retry/fallback row.
        assert queue.get_task_status(task_id) == "waiting_pool"
        assert queue.get_result(task_id) is None
        assert _own_events(queue, task_id) == before
        assert _events(queue, task_id, "result") == []
        assert _lineage(queue, task_id) == []
        assert dispatched == []
        # The next open resumes and dispatches the same row.
        path.write_text(_body("open"))
        assert _wait(lambda: dispatched == [task_id])
    assert len(_events(queue, task_id, "pool_hold_resumed")) == 1
    assert _lineage(queue, task_id) == []


def test_in_progress_wrapper_timeout_still_accepted(tmp_path, monkeypatch, notices):
    """Positive control: a dispatched row's wrapper timeout keeps the old path."""
    monkeypatch.setenv("AGENT_CREW_RETRY_MAX_ATTEMPTS", "0")
    _signal(tmp_path, monkeypatch, "open")
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    task_id = _impl(queue)
    with TestClient(app) as client:
        assert _wait(lambda: dispatched == [task_id])
        assert queue.get_task_status(task_id) == "in_progress"
        # The fake dispatcher stands in for the spawn only; record the
        # dispatch the real one commits before a worker can exist.
        record_dispatch_provenance(queue, task_id)
        resp = client.post(f"/tasks/{task_id}/result", json=_wrapper_timeout(task_id),
                           headers=HDR)
        assert resp.status_code == 200, resp.text
    assert queue.get_task_status(task_id) == "failed"
    assert len(_events(queue, task_id, "result")) == 1


def test_queue_refuses_parked_and_undispatched_wrapper_timeout(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    for tid in ("parked", "never"):
        queue.enqueue(TaskRequest(tid, "implement", "work", branch="main",
                                  context={"risk_tier": 1}))
    assert queue.park_pool_hold("parked", reason="hold")
    result = TaskResult(**_wrapper_timeout("parked"))
    with pytest.raises(ParkedResultRejected):
        queue.submit_result("parked", result)
    assert queue.get_task_status("parked") == "waiting_pool"
    # A pending row never handed to a worker cannot have timed out either.
    with pytest.raises(ParkedResultRejected):
        queue.submit_result("never", TaskResult(**_wrapper_timeout("never")))
    assert queue.get_task_status("never") == "pending"
    assert queue.get_result("never") is None


@pytest.mark.usefixtures("admit_crew_run")
def test_cli_wait_deadline_on_waiting_pool_posts_nothing(tmp_db, tmp_path, unused_tcp_port):
    from click.testing import CliRunner
    from agent_crew.cli import crew

    proj_dir = tmp_path / "myproj"
    proj_dir.mkdir()
    (proj_dir / "state.json").write_text(json.dumps({
        "project": "myproj", "port": unused_tcp_port, "port_file": str(proj_dir / "port"),
        "session": "test", "window": "0", "pane_ids": [], "pane_map": {},
        "agents": [], "worktrees": {}, "db": tmp_db, "server_pid": 0,
        "sessions_file": str(proj_dir / "sessions.json")}))
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest("run-parked-1", "implement", "parked", branch="main"))
    assert q.park_pool_hold("run-parked-1", reason="hold")

    posted = []

    def no_post(req, *a, **kw):
        url = str(getattr(req, "full_url", req))
        posted.append(url)
        raise OSError(f"no live server in this test: {url}")

    with patch("agent_crew.cli._port_listening", return_value=True), \
         patch("agent_crew.cli._verify_project_server"), \
         patch("agent_crew.cli._pane_alive", return_value=True), \
         patch("agent_crew.cli._verify_delivery", return_value=False), \
         patch("agent_crew.loop.enqueue_implement", return_value="run-parked-1"), \
         patch("agent_crew.cli._auto_detect_project", return_value=None), \
         patch("urllib.request.urlopen", side_effect=no_post):
        result = CliRunner().invoke(
            crew, ["run", "parked", "--project", "myproj", "--base", str(tmp_path),
                   "--timeout", "1", "--no-tester"], catch_exceptions=True)

    assert [p for p in posted if "/result" in str(p)] == []
    assert "parked waiting_pool; still queued" in result.output, result.output
    assert result.exit_code != 0
    assert q.get_task_status("run-parked-1") == "waiting_pool"
    assert q.get_result("run-parked-1") is None


def test_undispatched_pending_completed_result_is_409_over_http(tmp_path, monkeypatch):
    """Alfred option (a) 023138z: the worker-facing entry points refuse any result
    on a row with no dispatch provenance — a completed one included."""
    monkeypatch.delenv("AGENT_CREW_DISPATCHER", raising=False)
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    task_id = _impl(queue, task_id="impl-never")
    app = create_app(queue._db_path, watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        resp = client.post(f"/tasks/{task_id}/result",
                           json=_result(task_id, "completed", None), headers=HDR)
    assert resp.status_code == 409, resp.text
    assert resp.json()["prior_status"] == "pending"
    assert queue.get_task_status(task_id) == "pending"
    assert queue.get_result(task_id) is None
    assert _events(queue, task_id, "result") == []
    assert _lineage(queue, task_id) == []


def test_undispatched_pending_completed_result_is_refused_over_mcp(tmp_path, monkeypatch):
    from agent_crew.mcp_server import build_mcp_server

    queue = TaskQueue(str(tmp_path / "tasks.db"))
    task_id = _impl(queue, task_id="impl-never-mcp")
    monkeypatch.setattr("agent_crew.mcp_server.verify_implement_artifact",
                        lambda *_a, **_kw: (True, "verified"))
    tool = build_mcp_server(queue._db_path)._tool_manager._tools["submit_result"].fn
    out = tool(task_id=task_id, status="completed", summary="done",
               branch="agent/x", commit="a" * 40)
    if asyncio.iscoroutine(out):
        out = asyncio.run(out)
    assert out["acknowledged"] is False and out["parked"] is True, out
    assert out["prior_status"] == "pending"
    assert queue.get_task_status(task_id) == "pending"
    assert queue.get_result(task_id) is None
    assert _events(queue, task_id, "result") == []


def test_server_internal_terminal_write_is_not_refused(tmp_path):
    """The queue itself keeps the narrow rule for server-internal callers: the
    dispatcher ends claimed rows before ``record_dispatch`` (_fail_if_active)."""
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    task_id = _impl(queue, task_id="impl-internal")
    assert queue.dequeue(role="implementer").task_id == task_id
    queue.submit_result(task_id, TaskResult(**_result(task_id, "failed", "no_worktree")),
                        expected_status="in_progress", dispatcher_failed=True)
    assert queue.get_task_status(task_id) == "failed"
    with pytest.raises(ParkedResultRejected):
        queue.submit_result(task_id, TaskResult(**_result(task_id, "failed", "other")),
                            worker_entry=True)


def test_no_worktree_fail_if_active_still_terminates_the_row(tmp_path, monkeypatch):
    """A pre-dispatch dispatcher failure is a server-internal write: the row
    must end ``failed``, not stay ``in_progress`` with no lease (#1050 c7072954527)."""
    from tests.unit.test_issue_712_dispatcher_cloud import (
        _enqueue_review, _state, _wait_for, cloud)

    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _enqueue_review(queue)
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", ".02")
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setenv("AGENT_CREW_CLOUD_SHADOW", "1")
    monkeypatch.setattr(cloud, "launch_shadow_review", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cloud, "reconcile_shadow_reviews", lambda *_args: None)
    app = create_app(queue._db_path, state_path=_state(tmp_path, reviewer_worktree=False),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        _wait_for(lambda: queue.get_task("review-cloud").status == "failed")
    assert queue.get_result("review-cloud").error_info["reason"] == "no_worktree"
    assert _events(queue, "review-cloud", "dispatched") == []


def test_canary_suppression_still_completes_the_review_without_dispatch(tmp_path, monkeypatch):
    """The canary completes a review instead of dispatching it — a
    server-internal write the worker-entry refusal must not reach."""
    from agent_crew import tokenomics_canary as canary
    from tests.unit.test_sev0_tokenomics_canary import _dispatch_review

    spawned, row, _receipt, result, _stacks, _wt = _dispatch_review(
        tmp_path, monkeypatch, pin="impl-77")
    assert spawned == []
    assert row.status == "completed", row.status
    assert result is not None and result.summary == canary.SUPPRESSED_REASON
    assert _events(TaskQueue(str(tmp_path / "tasks.db")), row.task_id, "dispatched") == []
