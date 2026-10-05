"""#581: codex "Selected model is at capacity" backs off instead of
requeueing immediately.

Before: the transient branch requeued at once, so the default 3 retries
burned out in ~4 minutes. Now codex_capacity alone sets `push_not_before`
(base * 2**(n-1); 2/4/8/16 min by default) and the dispatcher's dequeue
honours it; after AGENT_CREW_CAPACITY_RETRY_MAX it fails as before. No
implementer fallback (owner policy: HOLD).
"""
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app

_CAPACITY_LOG = b"ERROR: Selected model is at capacity. Please try a different model.\n"


def _state(tmp_path) -> str:
    wts = {}
    for name in ("claude", "codex", "gemini"):
        wt = tmp_path / name
        wt.mkdir()
        (wt / ".git").mkdir()
        wts[name] = str(wt)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"worktrees": wts}))
    return str(state_file)


def _run(tmp_db, tmp_path, port, *, env: dict, succeed_on: int, task_id: str,
         wait_for: str, timeout: float = 10.0):
    """Dispatch one task whose first ``succeed_on - 1`` runs hit capacity."""
    spawn_times: list[float] = []

    async def fake_subprocess(*args, **kwargs):
        spawn_times.append(time.time())
        stdout = kwargs.get("stdout")
        proc = MagicMock()
        proc.kill = MagicMock()
        if len(spawn_times) < succeed_on:
            if stdout is not None:
                stdout.write(_CAPACITY_LOG)
                stdout.flush()
            proc.returncode = 1
        else:
            TaskQueue(tmp_db).submit_result(task_id, TaskResult(
                task_id=task_id, status="completed", summary="done"))
            proc.returncode = 0
        proc.wait = AsyncMock(return_value=proc.returncode)
        return proc

    with patch.dict(os.environ, {
        "AGENT_CREW_DISPATCHER": "1",
        "AGENT_CREW_DISPATCH_INTERVAL": "0.05",
        "AGENT_CREW_WORKTREE_SYNC_DISABLED": "1",
        **env,
    }):
        with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
            with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                app = create_app(db_path=tmp_db, pane_map={}, port=port,
                                 state_path=_state(tmp_path),
                                 watchdog_disabled=True, anomaly_disabled=True)
                with TestClient(app) as client:
                    resp = client.post("/tasks", json={
                        "task_id": task_id, "task_type": "test",
                        "description": "Test PR #1", "branch": "main",
                        "priority": 3, "context": {}, "project": "test_project"})
                    assert resp.status_code == 201
                    deadline = time.time() + timeout
                    status = None
                    while time.time() < deadline:
                        row = next((r for r in TaskQueue(tmp_db).list_all_with_status()
                                    if r["task_id"] == task_id), None)
                        status = row["status"] if row else None
                        if status == wait_for:
                            break
                        time.sleep(0.05)
    return status, spawn_times


def test_u_581_capacity_three_times_then_success_completes(tmp_db, tmp_path, *, unused_tcp_port):
    status, spawns = _run(tmp_db, tmp_path, unused_tcp_port,
                          env={"AGENT_CREW_CAPACITY_BACKOFF_S": "0.2"},
                          succeed_on=4, task_id="t-581-ok", wait_for="completed")
    assert status == "completed", f"status={status!r} spawns={len(spawns)}"
    assert len(spawns) == 4
    # Not requeued immediately: each retry waited at least its backoff.
    gaps = [b - a for a, b in zip(spawns, spawns[1:])]
    for gap, want in zip(gaps, (0.2, 0.4, 0.8)):
        assert gap >= want, f"retry gaps {gaps} shorter than backoff 0.2/0.4/0.8"


def test_u_581_capacity_exhausted_fails_as_before(tmp_db, tmp_path, *, unused_tcp_port):
    status, spawns = _run(tmp_db, tmp_path, unused_tcp_port,
                          env={"AGENT_CREW_CAPACITY_BACKOFF_S": "0.05",
                               "AGENT_CREW_CAPACITY_RETRY_MAX": "2"},
                          succeed_on=99, task_id="t-581-fail", wait_for="failed")
    assert status == "failed"
    assert len(spawns) == 3   # initial + 2 retries, then stop


def test_u_581_dispatcher_dequeue_honours_only_capacity_backoff(tmp_db):
    q = TaskQueue(tmp_db)
    future = time.time() + 600
    q.enqueue(TaskRequest(task_id="cap", task_type="test", description="d", branch="b-cap",
                          context={"push_not_before": future,
                                   "push_refusal_reason": "codex_capacity"}))
    q.enqueue(TaskRequest(task_id="pane", task_type="test", description="d", branch="b-pane",
                          context={"push_not_before": future,
                                   "push_refusal_reason": "pane_not_agent_shell"}))
    got = q.dequeue(role="tester", claimed_via="dispatcher")
    # The pane-refusal backoff is not the dispatcher's concern; capacity is.
    assert got is not None and got.task_id == "pane"
    assert q.dequeue(role="tester", claimed_via="dispatcher") is None


def test_u_581_mcp_and_http_poll_cannot_claim_capacity_deferred_task(tmp_db):
    q = TaskQueue(tmp_db)
    future = time.time() + 600
    q.enqueue(TaskRequest(task_id="cap", task_type="implement", description="d",
                          branch="b-cap", context={
                              "push_not_before": future,
                              "push_refusal_reason": "codex_capacity"}))
    q.enqueue(TaskRequest(task_id="pane", task_type="implement", description="d",
                          branch="b-pane", context={
                              "push_not_before": future,
                              "push_refusal_reason": "pane_not_agent_shell"}))
    # The pane's refusal is not a provider delay, so an independent consumer
    # may take it. The capacity task remains unavailable to both poll paths.
    got = q.dequeue(agent="codex", role="implementer", claimed_via="mcp")
    assert got is not None and got.task_id == "pane"
    assert q.dequeue(agent="codex", role="implementer", claimed_via="http_poll") is None


def test_u_581_discuss_claims_honor_capacity_but_not_pane_backoff(tmp_db):
    q = TaskQueue(tmp_db)
    future = time.time() + 600
    q.enqueue(TaskRequest(task_id="discuss-cap", task_type="discuss", description="d",
                          branch="b-cap", context={
                              "agent": "codex", "push_not_before": future,
                              "push_refusal_reason": "codex_capacity"}))
    q.enqueue(TaskRequest(task_id="discuss-pane", task_type="discuss", description="d",
                          branch="b-pane", context={
                              "agent": "codex", "push_not_before": future,
                              "push_refusal_reason": "pane_not_agent_shell"}))
    got = q.dequeue_discuss_for_agent("codex", claimed_via="dispatcher")
    assert got is not None and got.task_id == "discuss-pane"
    assert q.dequeue_discuss_for_agent("codex", claimed_via="mcp") is None
