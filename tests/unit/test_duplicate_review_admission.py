import json
import dataclasses

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.cea import store as receipt_store
from agent_crew.queue import DuplicateReviewError, TaskQueue
from agent_crew.server import create_app


SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture
def queue(tmp_path):
    return TaskQueue(str(tmp_path / "queue.db"))


def task(task_id, *, kind="review", pr=23, sha=SHA_A, project="owner/repo", branch="feature",
         allow_duplicate_review=False):
    context = {"allow_duplicate_review": allow_duplicate_review}
    if pr is not None:
        context["pr_number"] = pr
    if sha is not None:
        context["reviewed_sha"] = sha
    return TaskRequest(task_id=task_id, task_type=kind, description="check PR",
                       project=project, branch=branch,
                       context=context)


@pytest.mark.parametrize("kind", ["review", "test"])
@pytest.mark.parametrize("pr", [5694, None])
@pytest.mark.parametrize("second_sha", [SHA_A, None])
def test_alpha_engine_pending_unpinned_cascade_blocks_coordinator(queue, kind, pr,
                                                                   second_sha):
    first = task("review-impl-dartsub-0926b-r0", kind=kind, pr=pr, sha=None)
    second = task("review-5694-r1", kind=kind, pr=pr, sha=second_sha)
    second.context["coordinator_managed"] = True
    queue.enqueue(first)
    with pytest.raises(DuplicateReviewError) as exc:
        queue.enqueue(second)
    assert exc.value.existing_task_id == first.task_id


def test_reverse_order_waits_for_dispatch_pin(queue):
    queue.enqueue(task("pinned-first", sha=SHA_A))
    queue.enqueue(task("unpinned-second", sha=None))
    assert len(queue.list_tasks()) == 2
    first = queue.dequeue(role="reviewer")
    assert first.task_id == "pinned-first"
    assert queue.record_prepared_review_base(first.task_id, {"reviewed_sha": SHA_A})
    second = queue.dequeue(role="reviewer")
    assert second.task_id == "unpinned-second"
    assert not queue.record_prepared_review_base(second.task_id, {"reviewed_sha": SHA_A})
    assert queue.get_task_status(second.task_id) == "blocked"
    conn = queue._connect()
    try:
        receipt = receipt_store.current_receipt(conn, queue.task_receipt_id(second.task_id))
        assert receipt["state"] == "REVOKED"
        row = conn.execute("SELECT event, fields FROM task_exec_events "
                           "WHERE task_id=? AND event='duplicate_review_refused'",
                           (second.task_id,)).fetchone()
        assert json.loads(row["fields"])["existing_task_id"] == first.task_id
    finally:
        conn.close()


def test_dispatch_pin_override_and_different_sha(queue):
    queue.enqueue(task("first", sha=SHA_A))
    queue.enqueue(task("override", sha=None, allow_duplicate_review=True))
    first = queue.dequeue(role="reviewer")
    assert queue.record_prepared_review_base(first.task_id, {"reviewed_sha": SHA_A})
    override = queue.dequeue(role="reviewer")
    assert queue.record_prepared_review_base(override.task_id, {"reviewed_sha": SHA_A})
    assert queue.get_task_status(override.task_id) == "in_progress"
    conn = queue._connect()
    try:
        event = conn.execute("SELECT 1 FROM task_exec_events WHERE task_id='override' "
                             "AND event='duplicate_review_override'").fetchone()
        assert event is not None
    finally:
        conn.close()
    queue.enqueue(task("new-head", sha=None))
    different = queue.dequeue(role="reviewer")
    assert queue.record_prepared_review_base(different.task_id, {"reviewed_sha": SHA_B})
    assert queue.get_task_status(different.task_id) == "in_progress"


@pytest.mark.parametrize("kind", ["review", "test"])
def test_pending_unpinned_override_and_new_head(queue, kind):
    queue.enqueue(task("first", kind=kind, sha=None))
    queue.enqueue(task("override", kind=kind, sha=SHA_A, allow_duplicate_review=True))
    assert queue.get_task_status("override") == "pending"
    conn = queue._connect()
    try:
        event = conn.execute("SELECT 1 FROM task_exec_events WHERE task_id='override' "
                             "AND event='duplicate_review_override'").fetchone()
        assert event is not None
    finally:
        conn.close()


@pytest.mark.parametrize("kind", ["review", "test"])
def test_dispatch_pin_refuses_completed_judgement(queue, kind):
    queue.enqueue(task("judged", kind=kind, sha=SHA_A))
    queue.submit_result("judged", TaskResult(
        task_id="judged", status="completed", summary="done",
        verdict="approve" if kind == "review" else None))
    queue.enqueue(task("late-unpinned", kind=kind, sha=None))
    late = queue.dequeue(role="reviewer" if kind == "review" else "tester")
    assert late.task_id == "late-unpinned"
    assert not queue.record_prepared_review_base(late.task_id, {"reviewed_sha": SHA_A})
    assert queue.get_task_status(late.task_id) == "blocked"


def test_duplicate_found_at_prepared_base_is_not_pushed(tmp_path, monkeypatch):
    from agent_crew import server

    db = str(tmp_path / "push.db")
    q = TaskQueue(db)
    q.enqueue(task("judged", sha=SHA_A))
    q.submit_result("judged", TaskResult(
        task_id="judged", status="completed", summary="done", verdict="approve"))
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setenv("AGENT_CREW_DELIVERY", "push")
    monkeypatch.setattr(server, "_prepare_worktree_for_task", lambda *_a, **_kw: SHA_A)
    pushes = []
    app = create_app(
        db_path=db, pane_map={"reviewer": "%101"},
        worktree_map={"reviewer": str(tmp_path)}, port=9999,
        push_fn=lambda pane, text: pushes.append((pane, text)),
        watchdog_disabled=True, anomaly_disabled=True,
    )
    with TestClient(app) as client:
        response = client.post("/tasks", json=dataclasses.asdict(task("late", sha=None)))
        assert response.status_code == 201, response.text
    assert q.get_task_status("late") == "blocked"
    assert pushes == []


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
