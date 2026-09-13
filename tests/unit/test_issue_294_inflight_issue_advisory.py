"""#294 — a direct enqueue could not see work the watcher was already running.

#276 taught `watch.active_issue_numbers()` to see an issue named only in a
task's description, so the WATCHER stopped claiming an issue another path had
in flight. The guard lives in `watch.py` and nothing else consulted it, so the
protection was one-directional. Measured 2026-09-13:

    impl-watch-ac467aac  03:20:04  crew triage --watch  {"issue": 292, "source": "watch"}
    implement-066c91a2   03:31:38  direct enqueue       {"issue": 292}

The second was enqueued while the first was still `in_progress` and 15 minutes
before the resulting PR existed, and ran a full implementer invocation against
work already in progress. It even carried `context.issue` — the structured field
#276 relies on. Nothing on that path read it.

⛔Surfaced, not blocked, and the distinction is the design. Several tasks
  legitimately share one issue — the implement task, its review, its fix rounds,
  its test — so refusing duplicates by issue would break the cascade this repo
  is built on. Only a second *implement* for an issue that already has a
  non-terminal implement is the expensive case, and even that is advisory: the
  caller is told at the one moment the collision is cheap to act on.
"""

import json

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app
from agent_crew.watch import active_tasks_for_issue


def _server(tmp_db):
    return create_app(db_path=tmp_db, pane_map={}, port=0,
                      watchdog_disabled=True, anomaly_disabled=True,
                      push_fn=lambda *a, **k: None)


def _post(client, task_id, *, task_type="implement", issue=None, description="go",
          project="demo"):
    context = {} if issue is None else {"issue": issue}
    return client.post("/tasks", json={
        "task_id": task_id, "task_type": task_type, "description": description,
        "branch": "main", "priority": 3, "context": context, "project": project})


# ── 1. the lookup ─────────────────────────────────────────────────────


def test_an_in_flight_task_is_found(tmp_db):
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement",
                          description="go", branch="main", context={"issue": 292}))
    assert active_tasks_for_issue(q, 292) == ["impl-a"]


def test_a_terminal_task_is_not_in_flight(tmp_db):
    from agent_crew.protocol import TaskResult

    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement",
                          description="go", branch="main", context={"issue": 292}))
    q.submit_result("impl-a", TaskResult(task_id="impl-a", status="completed",
                                         summary="done"))
    assert active_tasks_for_issue(q, 292) == []


def test_the_description_fallback_is_shared_with_the_watcher(tmp_db):
    """⛔#276's parser, reused rather than reimplemented. Two copies of "which
    issue is this task about" would drift, and the drift would be invisible."""
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement",
                          description="Implement #292: something",
                          branch="main", context={"reviewed_sha": "abc"}))
    assert active_tasks_for_issue(q, 292) == ["impl-a"]


def test_the_task_type_filter(tmp_db):
    q = TaskQueue(tmp_db)
    for task_id, task_type in (("impl-a", "implement"), ("rev-a", "review")):
        q.enqueue(TaskRequest(task_id=task_id, task_type=task_type, description="go",
                              branch="main", context={"issue": 292}))
    assert active_tasks_for_issue(q, 292, task_type="implement") == ["impl-a"]
    assert active_tasks_for_issue(q, 292, task_type="review") == ["rev-a"]
    assert sorted(active_tasks_for_issue(q, 292)) == ["impl-a", "rev-a"]


def test_a_different_issue_does_not_match(tmp_db):
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement", description="go",
                          branch="main", context={"issue": 999}))
    assert active_tasks_for_issue(q, 292) == []


def test_an_unreadable_queue_is_not_an_error(tmp_db):
    """⛔Bookkeeping never breaks an enqueue. This is advisory information; a
    failure to gather it must not cost the caller their task."""
    class _Broken:
        def list_all_with_status(self):
            raise RuntimeError("db is on fire")

    assert active_tasks_for_issue(_Broken(), 292) == []


# ── 2. the advisory on POST /tasks ────────────────────────────────────


def test_the_reported_collision_is_now_surfaced(tmp_db):
    """★★The exact #294 scenario: a watch task in flight, then a direct
    enqueue for the same issue."""
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-watch-ac467aac", task_type="implement",
                          description="Implement #292: ...", branch="main",
                          context={"issue": 292, "source": "watch"}))
    with TestClient(_server(tmp_db)) as client:
        body = _post(client, "implement-066c91a2", issue=292).json()
    assert body["in_flight_for_issue"] == ["impl-watch-ac467aac"]


def test_the_task_is_still_enqueued(tmp_db):
    """⛔Advisory, not a gate. Blocking would break the cascade — and worse,
    would make the caller's decision for them at the one moment they have the
    context to make it themselves."""
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement", description="go",
                          branch="main", context={"issue": 292}))
    with TestClient(_server(tmp_db)) as client:
        response = _post(client, "impl-b", issue=292)
    assert response.status_code == 201
    assert "impl-b" in {t.task_id for t in TaskQueue(tmp_db).list_tasks()}


def test_the_collision_is_logged_with_the_in_flight_id(tmp_db, caplog):
    import logging

    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement", description="go",
                          branch="main", context={"issue": 292}))
    with caplog.at_level(logging.WARNING, logger="agent_crew.server"):
        with TestClient(_server(tmp_db)) as client:
            _post(client, "impl-b", issue=292)
    assert any("impl-a" in r.message and "292" in r.message
               for r in caplog.records if r.levelno >= logging.WARNING), caplog.text


def test_a_review_alongside_an_implement_is_not_a_collision(tmp_db):
    """⛔The cascade's normal shape. implement → review → fix → test all name one
    issue; flagging them would make the advisory noise, and noise is ignored."""
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement", description="go",
                          branch="main", context={"issue": 292}))
    with TestClient(_server(tmp_db)) as client:
        body = _post(client, "rev-a", task_type="review", issue=292).json()
    assert body["in_flight_for_issue"] == []


def test_a_finished_implement_is_not_a_collision(tmp_db):
    from agent_crew.protocol import TaskResult

    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-a", task_type="implement", description="go",
                          branch="main", context={"issue": 292}))
    q.submit_result("impl-a", TaskResult(task_id="impl-a", status="completed",
                                         summary="done"))
    with TestClient(_server(tmp_db)) as client:
        body = _post(client, "impl-b", issue=292).json()
    assert body["in_flight_for_issue"] == []


def test_a_task_with_no_issue_reports_no_collision(tmp_db):
    with TestClient(_server(tmp_db)) as client:
        body = _post(client, "impl-a").json()
    assert body["in_flight_for_issue"] == []


def test_the_key_is_always_present(tmp_db):
    """⛔Always a list, never absent. A consumer should not have to distinguish
    "no collision" from "this server version does not report collisions" — an
    advisory nobody can rely on finding is one nobody will read."""
    with TestClient(_server(tmp_db)) as client:
        first = _post(client, "impl-a", issue=292).json()
        second = _post(client, "impl-b", issue=292).json()
    assert first["in_flight_for_issue"] == []
    assert second["in_flight_for_issue"] == ["impl-a"]
    assert first["task_id"] == "impl-a" and second["task_id"] == "impl-b"


def test_the_new_task_does_not_report_itself(tmp_db):
    """The check runs before the enqueue, so the task being created can never
    appear in its own collision list."""
    with TestClient(_server(tmp_db)) as client:
        body = _post(client, "impl-solo", issue=292).json()
    assert "impl-solo" not in body["in_flight_for_issue"]


def test_a_bookkeeping_failure_does_not_break_the_enqueue(tmp_db, monkeypatch):
    """⛔The whole feature is advisory, so it must never be able to cost a task."""
    import agent_crew.server as sv

    monkeypatch.setattr(sv, "active_tasks_for_issue",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with TestClient(_server(tmp_db)) as client:
        response = _post(client, "impl-a", issue=292)
    assert response.status_code == 201
    assert response.json()["in_flight_for_issue"] == []
