"""Codex quota failures park work until reset or fresher pooled headroom."""

import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import (_detect_transient_error_in_log,
                               _codex_quota_reset_at, create_app)


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


def test_real_codex_message_is_classified_and_reset_parsed(tmp_path):
    log = tmp_path / "dispatch.log"
    log.write_text("ERROR: You've hit your usage limit. Try again at 3:10 PM.\n")
    assert _detect_transient_error_in_log(str(log)) == "codex_quota_exhausted"
    # 2026-10-09 13:02 UTC -> 15:10 UTC on the same day.
    failed_at = 1791550920.0
    assert _codex_quota_reset_at(str(log), failed_at=failed_at) == 1791558600.0


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
        kwargs["stdout"].write(b"ERROR: You've hit your usage limit. Try again at 3:10 PM.\n")
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
