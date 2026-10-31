"""#864: coordinator notice delivery and retryable cloud submit failures."""

import json
import sqlite3
import subprocess
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_crew import claude_cloud as cloud
from agent_crew import server
from agent_crew.pipeline import MAX_TASK_RETRIES
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


def _queue(tmp_path, *, attempt=0):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(
        "review-864", "review", "Review PR", branch="agent/864-work",
        project="agent_crew", context={
            "repo": "owner/repo", "pr_number": 864,
            "reviewed_sha": "a" * 40, "retry_attempt": attempt,
            "agent_override": "claude_cloud", "cloud_session_id": "old-session",
        }))
    return queue


def _events(queue, event):
    with sqlite3.connect(queue._db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM task_exec_events WHERE event=?", (event,)
        ).fetchone()[0]


def test_command_delivers_and_skips_telegram(tmp_path, monkeypatch):
    queue = _queue(tmp_path)
    monkeypatch.setenv("AGENT_CREW_COORDINATOR_NOTICE_CMD", "notice --post")
    calls = []
    monkeypatch.setattr(server.subprocess, "run", lambda argv, **kw:
                        calls.append((argv, kw)) or SimpleNamespace(returncode=0))
    monkeypatch.setattr("agent_crew.notify.notify_telegram",
                        lambda message: pytest.fail("Telegram should not run"))
    server.coordinator_notice_once(queue, "review-864", "stalled")
    assert calls[0][0] == ["notice", "--post"]
    assert "review-864" in calls[0][1]["input"]
    assert calls[0][1]["timeout"] == 15
    assert _events(queue, "coordinator_notice_delivered") == 1


@pytest.mark.parametrize("failure", ["rc", "timeout"])
def test_command_failure_falls_back_then_gives_up(tmp_path, monkeypatch, failure):
    queue = _queue(tmp_path)
    monkeypatch.setenv("AGENT_CREW_COORDINATOR_NOTICE_CMD", "notice")
    def run(argv, **kw):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 15)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(server.subprocess, "run", run)
    telegram = []
    monkeypatch.setattr("agent_crew.notify.notify_telegram",
                        lambda msg: telegram.append(msg) or False)
    monkeypatch.setattr("agent_crew.notify.notify_console", lambda msg: None)
    for _ in range(3):
        server.coordinator_notice_once(queue, "review-864", "stalled")
    assert len(telegram) == 3
    assert _events(queue, "coordinator_notice_failed") == 3
    assert _events(queue, "coordinator_notice_gave_up") == 1


def test_health_reports_missing_sink_and_recent_giveups(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("AGENT_CREW_COORDINATOR_NOTICE_CMD", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    queue = _queue(tmp_path)
    with sqlite3.connect(queue._db_path) as conn:
        conn.execute("INSERT INTO task_exec_events(task_id,event,at,fields) VALUES(?,?,?,?)",
                     ("review-864", "coordinator_notice_gave_up", time.time(), json.dumps({"kind":"a"})))
        conn.execute("INSERT INTO task_exec_events(task_id,event,at,fields) VALUES(?,?,?,?)",
                     ("review-864", "coordinator_notice_gave_up", time.time()-90000, json.dumps({"kind":"b"})))
    app = server.create_app(str(tmp_path / "tasks.db"), pane_map={},
                            watchdog_disabled=True, anomaly_disabled=True,
                            identity_required=False)
    with TestClient(app) as client:
        health = client.get("/health").json()
    assert health["coordinator_notice_sink"] == "none"
    assert health["coordinator_notices_gave_up_24h"] == 1
    assert "coordinator notice sink is none" in caplog.text


def _dispatched_review(queue):
    task = queue.dequeue(role="reviewer")
    queue.record_dispatch(task.task_id, channel=cloud.DISPATCH_CHANNEL,
                          agent=cloud.CLOUD_PROVIDER_NAME, target="pending")
    return queue.get_task(task.task_id)


def test_retryable_submit_failure_creates_one_review_retry(tmp_path, monkeypatch):
    queue = _queue(tmp_path)
    task = _dispatched_review(queue)
    notices = []
    monkeypatch.setattr(server, "_deliver_coordinator_notice",
                        lambda message: notices.append(message) or True)
    cloud._fail_closed_dispatch(queue, task, "review", "cloud_submit_timeout", retryable=True)
    retry = queue.get_task("retry-review-864-a1")
    assert retry.task_type == "review" and retry.branch == task.branch
    assert retry.context["pr_number"] == 864
    assert retry.context["reviewed_sha"] == "a" * 40
    assert retry.context["retry_of"] == task.task_id
    assert retry.context["retry_attempt"] == 1
    assert not any(key.startswith("cloud_") for key in retry.context)
    cloud._fail_closed_dispatch(queue, task, "review", "cloud_submit_timeout", retryable=True)
    assert len([t for t in queue.list_tasks() if t.task_id.startswith("retry-")]) == 1
    assert len(notices) == 1


def test_cloud_review_missing_pin_sends_one_coordinator_notice(tmp_path, monkeypatch):
    queue = _queue(tmp_path)
    task = _dispatched_review(queue)
    missing_pin = replace(task, context={"repo": "owner/repo", "pr_number": 864})
    notices = []
    monkeypatch.setattr(server, "_deliver_coordinator_notice",
                        lambda message: notices.append(message) or True)
    for _ in range(2):
        outcome = cloud._reconcile_cloud_review(
            queue, missing_pin, repo="owner/repo",
            pr_comments_fn=lambda *args, **kwargs: [],
            submit_review_result_fn=None)
        assert outcome.action == "failed"
    assert len(notices) == 1
    assert "cloud_review_missing_pinned_pr_or_sha" in notices[0]


@pytest.mark.parametrize("retryable,attempt,expected", [
    (True, MAX_TASK_RETRIES, True), (False, 0, False),
])
def test_retry_budget_and_nonretryable_failure(tmp_path, monkeypatch,
                                               retryable, attempt, expected):
    queue = _queue(tmp_path, attempt=attempt)
    task = _dispatched_review(queue)
    notices = []
    monkeypatch.setattr(server, "coordinator_notice_once",
                        lambda *args: notices.append(args))
    cloud._fail_closed_dispatch(queue, task, "review", "cloud_submit_timeout",
                                retryable=retryable)
    assert queue.get_task("retry-review-864-a1") is None
    assert bool(notices) is expected
