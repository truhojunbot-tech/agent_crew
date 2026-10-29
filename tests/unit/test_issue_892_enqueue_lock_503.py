"""#892: POST /tasks on a locked tasks DB must retry, then answer 503 with a reason.

alpha-engine lost four implement rounds on 10-28 to a bare ``HTTPError 500``
after ``sqlite3.OperationalError: database is locked`` — while ``/health`` said
200. A transient lock is retryable; the client has to be told so.
"""

import sqlite3

from fastapi.testclient import TestClient

from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


PROJECT = "issue892"


def _client(tmp_db):
    # Identity is on explicitly (as on a served dispatcher), so _post must carry
    # a matching X-Agent-Crew-Project or it stops at 428 before enqueue.
    return TestClient(create_app(db_path=tmp_db, project=PROJECT, identity_required=True,
                                 watchdog_disabled=True, anomaly_disabled=True,
                                 push_fn=lambda *a, **k: None),
                      raise_server_exceptions=False)


def _post(c, task_id):
    return c.post("/tasks", headers={"X-Agent-Crew-Project": PROJECT},
                  json={"task_id": task_id, "task_type": "review",
                        "description": "d", "branch": "b", "priority": 3,
                        "pr_number": 892,
                        "context": {"reviewed_sha": "a" * 40}, "project": PROJECT})


def _lock(monkeypatch, times):
    real = TaskQueue.enqueue
    calls = []

    def locked(self, task, *args, **kwargs):
        calls.append(task.task_id)
        if len(calls) <= times:
            raise sqlite3.OperationalError("database is locked")
        return real(self, task, *args, **kwargs)

    monkeypatch.setattr(TaskQueue, "enqueue", locked)
    return calls


def test_a_transient_lock_is_retried_to_one_task(tmp_db, monkeypatch):
    calls = _lock(monkeypatch, times=1)
    with _client(tmp_db) as c:
        r = _post(c, "lock-once")
    assert r.status_code == 201, r.text
    assert r.json() == {"task_id": "lock-once"}
    assert calls == ["lock-once", "lock-once"]
    assert [t.task_id for t in TaskQueue(tmp_db).list_tasks()] == ["lock-once"]


def test_a_persistent_lock_answers_503_with_reason_and_retry_after(tmp_db, monkeypatch):
    calls = _lock(monkeypatch, times=10**6)
    with _client(tmp_db) as c:
        r = _post(c, "lock-always")
    assert r.status_code == 503, r.text
    assert r.json() == {"detail": {"error": "tasks database is locked",
                                   "reason": "database_locked",
                                   "task_id": "lock-always", "retryable": True}}
    assert r.headers["Retry-After"].isdigit()
    assert len(calls) > 1
    assert TaskQueue(tmp_db).list_tasks() == []


def test_other_operational_errors_are_not_retried(tmp_db, monkeypatch):
    calls = []

    def broken(self, task, *args, **kwargs):
        calls.append(task.task_id)
        raise sqlite3.OperationalError("no such table: tasks")

    monkeypatch.setattr(TaskQueue, "enqueue", broken)
    with _client(tmp_db) as c:
        r = _post(c, "broken")
    assert r.status_code == 500
    assert calls == ["broken"]
