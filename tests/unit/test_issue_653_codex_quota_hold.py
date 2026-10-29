"""Codex quota failures park work until reset or fresher pooled headroom."""

import json
import os
import pytest
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.failure_kind import provider_failure_from_log
from agent_crew.server import create_app


def _queued(tmp_path, task_id):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(task_id, "implement", "work", context={"risk_tier": 1}))
    assert queue.dequeue(role="implementer", agent="codex").task_id == task_id
    return queue


def _cache(tmp_path, monkeypatch, *, fetched_at, fingerprint, remaining):
    root = tmp_path / "quota"
    path = root / "codex_monitor" / "quota_cache.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_at": fetched_at,
                                "account_fingerprint": fingerprint,
                                "pace": {"base_remaining_pct": remaining,
                                         "accounts": {"private@example.invalid": "do not read"}}}))
    monkeypatch.setenv("AGENT_CREW_CEA_QUOTA_CACHE_DIR", str(root))


def _marker(queue, task_id):
    return queue.get_task(task_id).error_info["codex_quota_hold"]


def _real_codex_quota_stream(reset="3:10 PM"):
    message = f"You’ve hit your usage limit. Try again at {reset}."
    return "\n".join((
        "[codex error] " + message,
        json.dumps({"type": "error", "message": message}, ensure_ascii=False),
        json.dumps({"type": "turn.failed", "error": {"message": message}},
                   ensure_ascii=False),
    )) + "\n"


@pytest.mark.parametrize("event_line", [1, 2])
def test_codex_quota_requires_provider_error_event(tmp_path, event_line):
    log = tmp_path / "dispatch.log"
    lines = _real_codex_quota_stream().splitlines()
    log.write_text(lines[0] + "\n")
    assert provider_failure_from_log(str(log), agent="codex") is None
    failed_at = datetime(2026, 10, 9, 13, 2, tzinfo=timezone.utc).timestamp()
    log.write_text(lines[event_line] + "\n")
    failure = provider_failure_from_log(str(log), agent="codex", failed_at=failed_at)
    assert failure == {"kind": "provider_limit",
                       "provider_code": "codex_quota_exhausted", "retry_after": 7680}


def test_codex_quota_dated_reset_is_parsed_from_provider_error(tmp_path):
    log = tmp_path / "dispatch.log"
    log.write_text(_real_codex_quota_stream("Sep 30th, 2026 4:48 PM"))
    failed_at = datetime(2026, 9, 24, 13, 2, tzinfo=timezone.utc).timestamp()
    reset_at = datetime(2026, 9, 30, 16, 48, tzinfo=timezone.utc).timestamp()
    failure = provider_failure_from_log(str(log), agent="codex", failed_at=failed_at)
    assert failure["kind"] == "provider_limit"
    assert failure["retry_after"] == reset_at - failed_at


def test_codex_quota_real_event_without_reset_still_holds(tmp_path):
    log = tmp_path / "dispatch.log"
    log.write_text(json.dumps({"type": "turn.failed", "error": {
        "message": "You've hit your usage limit."}}) + "\n")
    failure = provider_failure_from_log(str(log), agent="codex")
    assert failure == {"kind": "provider_limit",
                       "provider_code": "codex_quota_exhausted", "retry_after": None}


def test_fresher_cache_releases_one_probe_then_rest_without_duplicates(tmp_path, monkeypatch):
    queue = _queued(tmp_path, "quota-a")
    queue.enqueue(TaskRequest("quota-b", "implement", "work", context={"risk_tier": 1}))
    assert queue.dequeue(role="implementer", agent="codex").task_id == "quota-b"
    failed_at = time.time() - 10
    reset_at = time.time() + 3600
    for task_id in ("quota-a", "quota-b"):
        assert queue.park_codex_quota(task_id, failed_at=failed_at, reset_at=reset_at,
                                      account_fingerprint="old")
    _cache(tmp_path, monkeypatch, fetched_at=time.time(), fingerprint="new", remaining=0)
    app = create_app(queue.db_path, pane_map={}, watchdog_disabled=True,
                     anomaly_disabled=True)
    with TestClient(app):
        assert app.state.resume_codex_quota_holds() == ["quota-a"]
        assert queue.get_task_status("quota-a") == "pending"
        assert queue.get_task_status("quota-b") == "blocked"
        assert app.state.resume_codex_quota_holds() == []
        assert queue.dequeue(role="implementer", agent="codex").task_id == "quota-a"
        queue.record_dispatch("quota-a", channel="codex_exec", agent="codex")
        assert app.state.resume_codex_quota_holds() == []
        queue.bind_dispatch_target("quota-a", target="pid:1234")
        queue.submit_result("quota-a", TaskResult(task_id="quota-a", status="completed",
                                                  summary="probe finished"))
        assert queue.get_task("quota-a").error_info is None
        assert app.state.resume_codex_quota_holds() == ["quota-b"]
        assert app.state.resume_codex_quota_holds() == []
        assert queue.get_task_status("quota-b") == "pending"


def test_reset_releases_parked_task_when_cache_missing(tmp_path, monkeypatch):
    queue = _queued(tmp_path, "quota-reset")
    signed_context = queue.get_task_context("quota-reset")
    monkeypatch.delenv("AGENT_CREW_CEA_QUOTA_CACHE_DIR", raising=False)
    assert queue.park_codex_quota("quota-reset", failed_at=time.time() - 120,
                                  reset_at=time.time() - 1, account_fingerprint="old")
    app = create_app(queue.db_path, pane_map={}, watchdog_disabled=True,
                     anomaly_disabled=True)
    with TestClient(app):
        assert app.state.resume_codex_quota_holds() == ["quota-reset"]
        assert app.state.resume_codex_quota_holds() == []
    # The signed receipt binds the task context; internal hold bookkeeping
    # must not change that payload before the next claim.
    assert queue.get_task_context("quota-reset") == signed_context
    assert _marker(queue, "quota-reset")["probe"] is True


def test_stop_keeps_ready_quota_hold_blocked(tmp_path, monkeypatch):
    queue = _queued(tmp_path, "quota-stopped")
    monkeypatch.delenv("AGENT_CREW_CEA_QUOTA_CACHE_DIR", raising=False)
    queue.park_codex_quota("quota-stopped", failed_at=time.time() - 120,
                           reset_at=time.time() - 1, account_fingerprint="old")
    queue.set_stop_epoch(True, incident="test-stop")
    app = create_app(queue.db_path, pane_map={}, watchdog_disabled=True,
                     anomaly_disabled=True)
    with TestClient(app):
        assert app.state.resume_codex_quota_holds() == []
    assert queue.get_task_status("quota-stopped") == "blocked"


def test_dispatcher_tick_sees_fresh_cache_before_reset(tmp_path, monkeypatch):
    queue = _queued(tmp_path, "quota-tick")
    failed_at = time.time() - 10
    reset_at = time.time() + 3600
    queue.park_codex_quota("quota-tick", failed_at=failed_at,
                           reset_at=reset_at, account_fingerprint="old")
    _cache(tmp_path, monkeypatch, fetched_at=time.time(), fingerprint="old", remaining=78)
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", "0.02")
    app = create_app(queue.db_path, pane_map={}, worktree_map={},
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        deadline = time.monotonic() + 3
        while (not any(e["event"] == "codex_quota_resumed"
                       for e in queue.get_exec_state("quota-tick")["events"])
               and time.monotonic() < deadline):
            time.sleep(0.02)
    resumed = [e for e in queue.get_exec_state("quota-tick")["events"]
               if e["event"] == "codex_quota_resumed"]
    assert len(resumed) == 1 and resumed[0]["at"] < reset_at


def test_dispatcher_parks_real_quota_failure_without_fallback(tmp_path, monkeypatch,
                                                              unused_tcp_port):
    worktree = tmp_path / "codex"
    worktree.mkdir()
    (worktree / ".git").mkdir()
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"worktrees": {"codex": str(worktree)}}))
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest("quota-dispatch", "implement", "work", branch="main",
                              context={"risk_tier": 1}))
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", "0.02")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")

    async def fake_subprocess(*args, **kwargs):
        kwargs["stdout"].write(_real_codex_quota_stream().encode())
        kwargs["stdout"].flush()
        proc = MagicMock(returncode=1, pid=os.getpid())
        proc.wait = AsyncMock(return_value=1)
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
        app = create_app(db, pane_map={}, port=unused_tcp_port, state_path=str(state),
                         watchdog_disabled=True, anomaly_disabled=True)
        with TestClient(app):
            deadline = time.monotonic() + 5
            while queue.get_task_status("quota-dispatch") != "blocked" and time.monotonic() < deadline:
                time.sleep(0.02)
    assert queue.get_task_status("quota-dispatch") == "blocked"
    assert isinstance(_marker(queue, "quota-dispatch")["reset_at"], float)
    assert [t.task_id for t in queue.list_tasks() if t.task_id.startswith("fallback-")] == []
