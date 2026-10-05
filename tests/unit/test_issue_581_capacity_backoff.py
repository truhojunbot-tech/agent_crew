"""#581: a provider capacity refusal (codex "Selected model is at capacity")
cools down and requeues instead of retrying at once or failing.

- Classified as ``provider_capacity``: cooldown 2 → 4 → 8 min, capped at
  15 min, ±20% jitter, via ``push_not_before``.
- Never spends the generic transient budget and never ends the task: it is
  re-dispatched automatically whenever the cooldown expires.
- Every claim path (dispatcher, tmux push, MCP, HTTP poll) honours the
  cooldown (review r0 of PR #585).
- After 60 min of continuous refusals: ERROR log + one
  ``provider_capacity_blocked`` exec event; retries continue.
- Each capacity requeue is recorded with ``waste_reason=provider_capacity``.
- No account rotation, no Claude failover, no model change.
"""
import json
import logging
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import PROVIDER_CAPACITY, TaskQueue
from agent_crew.server import (
    _PROVIDER_CAPACITY_TAGS,
    _TRANSIENT_RETRIABLE_TAGS,
    _capacity_cooldown_s,
    _detect_transient_error_in_log,
    create_app,
)

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


def _events(db, task_id, name):
    return [e for e in TaskQueue(db).get_exec_state(task_id)["events"] if e["event"] == name]


# ── classification and cooldown schedule ─────────────────────────────────

def test_u_581_codex_capacity_is_provider_capacity_not_transient(tmp_path):
    log = tmp_path / "log"
    log.write_bytes(_CAPACITY_LOG)
    tag = _detect_transient_error_in_log(str(log))
    assert tag in _PROVIDER_CAPACITY_TAGS
    assert tag not in _TRANSIENT_RETRIABLE_TAGS   # cannot reach the transient budget


def test_u_581_cooldown_doubles_caps_at_15_min_with_jitter(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CAPACITY_JITTER", "0")
    assert [_capacity_cooldown_s(n) for n in range(1, 7)] == [120, 240, 480, 900, 900, 900]
    monkeypatch.setenv("AGENT_CREW_CAPACITY_JITTER", "0.2")
    first = [_capacity_cooldown_s(1) for _ in range(200)]
    assert all(96 <= d <= 144 for d in first) and len(set(first)) > 1
    assert all(d <= 900 for d in (_capacity_cooldown_s(40) for _ in range(200)))


# ── dispatcher end to end ────────────────────────────────────────────────

def _run(tmp_db, tmp_path, port, *, env: dict, succeed_on: int, task_id: str,
         timeout: float = 15.0):
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
        "AGENT_CREW_CAPACITY_JITTER": "0",
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
                        if status in ("completed", "failed"):
                            break
                        time.sleep(0.05)
    return status, spawn_times


def test_u_581_capacity_n_times_then_success_completes_without_transient_budget(
        tmp_db, tmp_path, caplog, *, unused_tcp_port):
    """Capacity 5x with the transient budget at 1: never fails, completes on
    the 6th run, each retry waits out its (capped) cooldown, and the 60-min
    blocked event fires exactly once (threshold shrunk for the test)."""
    caplog.set_level(logging.WARNING, logger="agent_crew.server")
    status, spawns = _run(tmp_db, tmp_path, unused_tcp_port, env={
        "AGENT_CREW_TRANSIENT_RETRY_MAX": "1",
        "AGENT_CREW_CAPACITY_BACKOFF_S": "0.1",
        "AGENT_CREW_CAPACITY_BACKOFF_CAP_S": "0.3",
        "AGENT_CREW_CAPACITY_BLOCKED_AFTER_S": "0.5",
    }, succeed_on=6, task_id="t-581-ok")
    assert status == "completed", f"status={status!r} spawns={len(spawns)}"
    assert len(spawns) == 6
    gaps = [b - a for a, b in zip(spawns, spawns[1:])]
    for gap, want in zip(gaps, (0.1, 0.2, 0.3, 0.3, 0.3)):
        assert gap >= want, f"retry gaps {gaps} shorter than cooldown 0.1/0.2/0.3(cap)"

    requeues = _events(tmp_db, "t-581-ok", "requeued")
    assert len(requeues) == 5
    assert all(e["waste_reason"] == PROVIDER_CAPACITY and e["waste_class"] == "infra"
               for e in requeues)
    assert [e["capacity_refusals"] for e in requeues] == [1, 2, 3, 4, 5]
    blocked = _events(tmp_db, "t-581-ok", "provider_capacity_blocked")
    assert len(blocked) == 1 and blocked[0]["blocked_for_s"] >= 0.5
    assert any(r.levelno == logging.ERROR and "provider_capacity" in r.getMessage()
               for r in caplog.records)


def test_u_581_transient_budget_still_applies_to_other_tags(tmp_db, tmp_path, *, unused_tcp_port):
    """Control: the generic budget is untouched, not removed."""
    spawns = []

    async def fake_subprocess(*args, **kwargs):
        spawns.append(1)
        kwargs["stdout"].write(b"Error: timeout waiting for response\n")
        kwargs["stdout"].flush()
        proc = MagicMock(returncode=1, kill=MagicMock())
        proc.wait = AsyncMock(return_value=1)
        return proc

    with patch.dict(os.environ, {"AGENT_CREW_DISPATCHER": "1", "AGENT_CREW_DISPATCH_INTERVAL": "0.05",
                                 "AGENT_CREW_WORKTREE_SYNC_DISABLED": "1",
                                 "AGENT_CREW_TRANSIENT_RETRY_MAX": "1"}), \
            patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess), \
            patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
        app = create_app(db_path=tmp_db, pane_map={}, port=unused_tcp_port,
                         state_path=_state(tmp_path), watchdog_disabled=True,
                         anomaly_disabled=True)
        with TestClient(app) as client:
            client.post("/tasks", json={"task_id": "t-581-agy", "task_type": "test",
                                        "description": "d", "branch": "main", "priority": 3,
                                        "context": {}, "project": "test_project"})
            deadline = time.time() + 5
            while time.time() < deadline:
                if TaskQueue(tmp_db).get_task_status("t-581-agy") == "failed":
                    break
                time.sleep(0.05)
    assert TaskQueue(tmp_db).get_task_status("t-581-agy") == "failed"
    assert len(spawns) == 2


# ── every claim path honours the cooldown ────────────────────────────────

def _deferred(q: TaskQueue, task_id: str, *, task_type="implement", context=None):
    q.enqueue(TaskRequest(task_id=task_id, task_type=task_type, description="d",
                          branch=f"b-{task_id}", context=dict(context or {})))
    assert q.dequeue(role={"implement": "implementer", "test": "tester"}[task_type]).task_id == task_id
    q.defer_provider_capacity(task_id, delay_s=600, blocked_after_s=3600,
                              provider_tag="codex_capacity")
    assert q.get_task_status(task_id) == "pending"


@pytest.mark.parametrize("kwargs", [
    dict(agent="codex", role="implementer", claimed_via="dispatcher", claim_source="dispatcher"),
    dict(role="implementer", claimed_via="tmux_push", skip_deferred=True),
    dict(agent="codex", role="implementer", claimed_via="mcp"),
    dict(role="implementer", claimed_via="http_poll"),
    dict(claimed_via="http_poll"),
], ids=["dispatcher", "tmux_push", "mcp", "http_poll", "any"])
def test_u_581_dequeue_honours_capacity_on_every_path(tmp_db, kwargs):
    q = TaskQueue(tmp_db)
    _deferred(q, "cap")
    assert q.dequeue(**kwargs) is None
    q.patch_context("cap", {"push_not_before": time.time() - 1})   # cooldown over
    assert q.dequeue(**kwargs).task_id == "cap"                     # re-dispatched


def test_u_581_agent_override_stage_honours_capacity(tmp_db):
    q = TaskQueue(tmp_db)
    _deferred(q, "ovr", context={"agent_override": "codex"})
    assert q.dequeue(agent="codex", claimed_via="dispatcher") is None




def test_u_581_http_poll_and_mcp_endpoints_honour_capacity(tmp_db):
    q = TaskQueue(tmp_db)
    _deferred(q, "ep")
    with patch.dict(os.environ, {"AGENT_CREW_DELIVERY": "both"}):
        app = create_app(db_path=tmp_db, pane_map=None, watchdog_disabled=True)
        with TestClient(app) as client:
            assert client.get("/tasks/next?role=implementer").json() is None
    from agent_crew.mcp_server import build_mcp_server
    fn = build_mcp_server(tmp_db)._tool_manager._tools["get_next_task"].fn
    assert fn(agent="codex") is None
    assert q.get_task_status("ep") == "pending"




# ── reviewed in b12c174 / 5dcefef (r0 finding): kept as-is ─────────────

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


# ── 60-minute blocked event ──────────────────────────────────────────────

def test_u_581_blocked_event_fires_once_after_60_min_and_keeps_retrying(tmp_db):
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="blk", task_type="implement", description="d", branch="b-blk"))

    def refuse():
        q.patch_context("blk", {"push_not_before": 0})
        assert q.dequeue(role="implementer").task_id == "blk"
        return q.defer_provider_capacity("blk", delay_s=0, blocked_after_s=3600,
                                         provider_tag="codex_capacity")

    first = refuse()
    assert first["count"] == 1 and not first["blocked"]
    assert _events(tmp_db, "blk", "provider_capacity_blocked") == []
    # 61 minutes of continuous refusals later…
    q.patch_context("blk", {"provider_capacity_since": time.time() - 3660})
    second = refuse()
    assert second["blocked"] and second["count"] == 2
    third = refuse()
    assert not third["blocked"] and third["count"] == 3     # once, not every time
    blocked = _events(tmp_db, "blk", "provider_capacity_blocked")
    assert len(blocked) == 1
    assert blocked[0]["waste_reason"] == PROVIDER_CAPACITY and blocked[0]["blocked_for_s"] >= 3600
    assert q.get_task_status("blk") == "pending"                  # still retrying


def test_u_581_capacity_keys_do_not_change_the_receipt_payload_hash():
    from agent_crew.cea.signed_receipt import payload_hash
    base = dict(task_type="implement", branch="b", description="d")
    assert payload_hash(**base, context={"x": 1}) == payload_hash(**base, context={
        "x": 1, "push_not_before": 1.0, "push_refusal_reason": "codex_capacity",
        "provider_capacity_count": 3, "provider_capacity_since": 1.0,
        "provider_capacity_blocked_at": 2.0})
