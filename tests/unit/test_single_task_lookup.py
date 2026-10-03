"""Single-task HTTP paths must not materialize the entire task table."""

import dataclasses
import json
import sqlite3

from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def test_get_task_matches_list_mapping_and_missing_is_none(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("impl-one", "implement", "change", branch="feature",
                              context={"nested": {"answer": 42}}))
    queue.submit_result("impl-one", TaskResult(
        "impl-one", "failed", "worker reported failure",
        findings=["detail"], error_info={"reason": "worker_error"}))

    listed = next(task for task in queue.list_tasks() if task.task_id == "impl-one")
    assert queue.get_task("impl-one") == listed
    assert queue.get_task("does-not-exist") is None


def test_fallback_lookup_filters_by_original_and_prefix(tmp_db):
    queue = TaskQueue(tmp_db)
    for task_id, original in (
        ("fallback-one", "impl-one"), ("fallback-two", "impl-two"),
        ("regular-one", "impl-one"),
    ):
        queue.enqueue(TaskRequest(task_id, "implement", "change", branch="feature",
                                  context={"original_task_id": original}))
    expected = [task for task in queue.list_tasks()
                if task.task_id.startswith("fallback-")
                and task.context.get("original_task_id") == "impl-one"]
    assert queue.list_fallback_tasks_for_original("impl-one") == expected
    assert queue.list_fallback_tasks_for_original("missing") == []


def test_get_task_http_keeps_fields_and_404_without_list_scan(tmp_db, monkeypatch):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("impl-http", "implement", "change", branch="feature"))
    expected = dataclasses.asdict(queue.get_task("impl-http"))

    def forbid_list(*_args, **_kwargs):
        raise AssertionError("single-task HTTP path called list_tasks")

    app = create_app(tmp_db, watchdog_disabled=True, anomaly_disabled=True,
                     pane_map={}, worktree_map={})
    with TestClient(app) as client:
        monkeypatch.setattr(TaskQueue, "list_tasks", forbid_list)
        found = client.get("/tasks/impl-http")
        missing = client.get("/tasks/not-here")
    assert found.status_code == 200
    assert {key: found.json()[key] for key in expected} == expected
    assert "execution" in found.json()
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Task 'not-here' not found"}


def test_result_http_uses_no_full_task_scan_including_fallback_check(
        tmp_db, monkeypatch):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("impl-result", "implement", "change", branch="feature"))
    queue.enqueue(TaskRequest("fallback-done", "implement", "fallback", branch="feature",
                              context={"original_task_id": "impl-result"}))
    with sqlite3.connect(tmp_db) as conn:
        conn.execute("UPDATE tasks SET status='failed' WHERE task_id='impl-result'")
        conn.execute("UPDATE tasks SET status='completed', context=? WHERE task_id='fallback-done'",
                     (json.dumps({"original_task_id": "impl-result"}),))
    assert [t.task_id for t in queue.list_fallback_tasks_for_original("impl-result")] == ["fallback-done"]

    def forbid_list(*_args, **_kwargs):
        raise AssertionError("result HTTP path called list_tasks")

    app = create_app(tmp_db, watchdog_disabled=True, anomaly_disabled=True,
                     pane_map={}, worktree_map={})
    with TestClient(app) as client:
        monkeypatch.setattr(TaskQueue, "list_tasks", forbid_list)
        monkeypatch.setattr("agent_crew.server._pipeline_auto_enqueue_review",
                            lambda *_args, **_kwargs: None)
        result = client.post("/tasks/impl-result/result", json={
            "task_id": "impl-result", "status": "completed", "summary": "corrected"})
    assert result.status_code == 200, result.text
    assert result.json() == {"status": "ignored_late_result",
                             "fallback_task_id": "fallback-done"}
    assert queue.get_task("impl-result").status == "failed"


def test_result_http_failed_path_uses_no_full_task_scan(tmp_db, monkeypatch):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("impl-failed", "implement", "change", branch="feature",
                              context={"retry_attempt": 2}))

    def forbid_list(*_args, **_kwargs):
        raise AssertionError("failed-result HTTP path called list_tasks")

    app = create_app(tmp_db, watchdog_disabled=True, anomaly_disabled=True,
                     pane_map={}, worktree_map={})
    with TestClient(app) as client:
        monkeypatch.setattr(TaskQueue, "list_tasks", forbid_list)
        result = client.post("/tasks/impl-failed/result", json={
            "task_id": "impl-failed", "status": "failed", "summary": "worker error"})
    assert result.status_code == 200, result.text
    assert result.json() == {"status": "ok"}
    assert queue.get_task("impl-failed").status == "failed"
