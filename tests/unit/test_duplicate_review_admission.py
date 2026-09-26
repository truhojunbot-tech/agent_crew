import json

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import DuplicateReviewError, TaskQueue
from agent_crew.server import create_app


SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture
def queue(tmp_path):
    return TaskQueue(str(tmp_path / "queue.db"))


def task(task_id, *, kind="review", pr=23, sha=SHA_A, project="owner/repo", branch="feature",
         allow_duplicate_review=False):
    return TaskRequest(task_id=task_id, task_type=kind, description="check PR",
                       project=project, branch=branch,
                       context={"pr_number": pr, "reviewed_sha": sha,
                                "allow_duplicate_review": allow_duplicate_review})


@pytest.mark.parametrize("kind", ["review", "test"])
def test_same_head_is_refused_and_records_event(queue, kind):
    queue.enqueue(task("first", kind=kind))
    with pytest.raises(DuplicateReviewError) as exc:
        queue.enqueue(task("second", kind=kind))
    assert exc.value.existing_task_id == "first"
    assert [t.task_id for t in queue.list_tasks()] == ["first"]
    conn = queue._connect()
    try:
        row = conn.execute("SELECT event, fields FROM task_exec_events WHERE task_id='second'").fetchone()
        assert row["event"] == "duplicate_review_refused"
        assert json.loads(row["fields"])["existing_task_id"] == "first"
    finally:
        conn.close()


def test_new_head_and_other_pr_are_allowed(queue):
    queue.enqueue(task("first"))
    queue.enqueue(task("new-head", sha=SHA_B))
    queue.enqueue(task("other-pr", pr=24))
    assert len(queue.list_tasks()) == 3


def test_project_type_and_branch_are_part_of_identity(queue):
    queue.enqueue(task("first"))
    queue.enqueue(task("other-project", project="other/repo"))
    queue.enqueue(task("other-type", kind="test"))
    queue.enqueue(task("branch-one", pr=None, branch="one"))
    queue.enqueue(task("branch-two", pr=None, branch="two"))


def test_in_progress_review_is_refused(queue):
    queue.enqueue(task("first"))
    assert queue.dequeue(role="reviewer").task_id == "first"
    with pytest.raises(DuplicateReviewError):
        queue.enqueue(task("second"))


@pytest.mark.parametrize("verdict", ["approve", "request_changes", "changes_requested"])
def test_completed_verdict_blocks_rereview(queue, verdict):
    queue.enqueue(task("first"))
    queue.submit_result("first", TaskResult(task_id="first", status="completed",
                                            summary="reviewed", verdict=verdict))
    with pytest.raises(DuplicateReviewError):
        queue.enqueue(task("second"))


def test_completed_test_without_verdict_blocks_retest(queue):
    queue.enqueue(task("first", kind="test"))
    queue.submit_result("first", TaskResult(task_id="first", status="completed",
                                            summary="tested"))
    with pytest.raises(DuplicateReviewError):
        queue.enqueue(task("second", kind="test"))


def test_refusal_does_not_commit_other_transaction_writes(queue, monkeypatch):
    queue.enqueue(task("first"))
    original = TaskQueue._duplicate_review_in_txn

    def with_prior_write(conn, task_request, context):
        conn.execute("UPDATE tasks SET description='unrelated write' WHERE task_id='first'")
        return original(conn, task_request, context)

    monkeypatch.setattr(TaskQueue, "_duplicate_review_in_txn", staticmethod(with_prior_write))
    with pytest.raises(DuplicateReviewError):
        queue.enqueue(task("second"))
    conn = queue._connect()
    try:
        description = conn.execute("SELECT description FROM tasks WHERE task_id='first'").fetchone()[0]
        event = conn.execute("SELECT event FROM task_exec_events WHERE task_id='second'").fetchone()[0]
        assert description == "check PR"
        assert event == "duplicate_review_refused"
    finally:
        conn.close()


def test_explicit_same_head_rereview_records_override(queue):
    queue.enqueue(task("first"))
    queue.enqueue(task("second", allow_duplicate_review=True))
    assert [t.task_id for t in queue.list_tasks()] == ["first", "second"]
    conn = queue._connect()
    try:
        row = conn.execute("SELECT event, fields FROM task_exec_events "
                           "WHERE task_id='second' AND event='duplicate_review_override'").fetchone()
        assert row is not None
        assert json.loads(row["fields"])["existing_task_id"] == "first"
    finally:
        conn.close()


def test_failed_or_unjudged_completion_can_retry(queue):
    queue.enqueue(task("first"))
    queue.submit_result("first", TaskResult(task_id="first", status="completed",
                                            summary="no verdict"))
    queue.enqueue(task("second"))


@pytest.mark.parametrize("kind", ["review", "test"])
@pytest.mark.parametrize("status", ["failed", "timed_out", "cancelled", "needs_human"])
def test_nonstanding_review_can_retry(queue, status, kind):
    queue.enqueue(task("first", kind=kind))
    conn = queue._connect()
    try:
        conn.execute("UPDATE tasks SET status=? WHERE task_id='first'", (status,))
        conn.commit()
    finally:
        conn.close()
    queue.enqueue(task("second", kind=kind))
    assert [t.task_id for t in queue.list_tasks()] == ["first", "second"]


def test_implement_tasks_unaffected(queue):
    queue.enqueue(task("first", kind="implement"))
    queue.enqueue(task("second", kind="implement"))


def test_http_refusal_names_existing_task(tmp_path):
    db = str(tmp_path / "api.db")
    with TestClient(create_app(db_path=db, watchdog_disabled=True,
                               anomaly_disabled=True, push_fn=lambda *a, **k: None)) as client:
        first = task("first")
        second = task("second")
        assert client.post("/tasks", json=vars(first)).status_code == 201
        response = client.post("/tasks", json=vars(second))
    assert response.status_code == 409
    assert response.json() == {"detail": {"error": "DUPLICATE_REVIEW",
                                          "existing_task_id": "first"}}
