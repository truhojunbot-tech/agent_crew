"""Result and cascade behavior with dispatcher-owned delivery."""
from fastapi.testclient import TestClient
import pytest

pytestmark = pytest.mark.usefixtures("pinned_task_transport")

from agent_crew.server import create_app
from agent_crew.queue import TaskQueue
from agent_crew.protocol import TaskRequest


def _task_payload(task_id="t1", task_type="implement", description="do work", priority=3, project=""):
    return {
        "task_id": task_id,
        "task_type": task_type,
        "description": description,
        "branch": "main",
        "priority": priority,
        "context": {"risk_tier": 1},
        "project": project,
    }


def _result_payload(task_id, status="completed", summary="done"):
    return {
        "task_id": task_id,
        "status": status,
        "summary": summary,
        "verdict": None,
        "findings": [],
        "pr_number": None,
    }


class RecordingPush:
    def __init__(self):
        self.calls = []

    def __call__(self, pane_id, text):
        self.calls.append((pane_id, text))


def test_task_enqueue_never_writes_to_a_pane(tmp_db):
    push = RecordingPush()
    app = create_app(db_path=tmp_db, pane_map={"implementer": "%91"}, push_fn=push)
    with TestClient(app) as client:
        assert client.post("/tasks", json=_task_payload("pane-free")).status_code == 201
    assert push.calls == []
    assert TaskQueue(tmp_db).get_task_status("pane-free") == "pending"


def test_u_sp14_impl_completed_auto_enqueue_review(tmp_db):
    app = create_app(db_path=tmp_db, pane_map={})
    with TestClient(app) as client:
        impl = _task_payload("impl-001", "implement", "Add feature X")
        impl["context"]["crew_run_branch"] = True
        assert client.post("/tasks", json=impl).status_code == 201
        assert client.post("/tasks/impl-001/result", json=_result_payload("impl-001")).status_code == 200
    reviews = [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "review"]
    assert len(reviews) == 1
    assert reviews[0].context["prev_task_id"] == "impl-001"



# U-SP15: impl task failed → no auto review enqueue
def test_u_sp15_impl_failed_no_auto_review(tmp_db):
    app = create_app(db_path=tmp_db, pane_map={})
    with TestClient(app) as client:
        assert client.post("/tasks", json=_task_payload("impl-002")).status_code == 201
        assert client.post("/tasks/impl-002/result", json=_result_payload("impl-002", status="failed")).status_code == 200
    assert [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "review"] == []



# U-SP16: impl task needs_human → no auto review enqueue
def test_u_sp16_impl_needs_human_no_auto_review(tmp_db):
    app = create_app(db_path=tmp_db, pane_map={})
    with TestClient(app) as client:
        assert client.post("/tasks", json=_task_payload("impl-003")).status_code == 201
        assert client.post("/tasks/impl-003/result", json=_result_payload("impl-003", status="needs_human")).status_code == 200
    assert [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "review"] == []



# U-SP17: auto-enqueued review task can be queried via /tasks (it gets pushed, so check in_progress)
def test_u_sp17_auto_review_task_queryable(tmp_db):
    app = create_app(db_path=tmp_db, pane_map={})
    with TestClient(app) as client:
        impl = _task_payload("impl-004", "implement", "Fix bug Y")
        impl["context"]["crew_run_branch"] = True
        assert client.post("/tasks", json=impl).status_code == 201
        assert client.post("/tasks/impl-004/result", json=_result_payload("impl-004")).status_code == 200
        reviews = [t for t in client.get("/tasks").json() if t["task_type"] == "review"]
    assert len(reviews) == 1
    assert reviews[0]["context"]["prev_task_id"] == "impl-004"



# U-SP18: dead pane before push → task rolled back to queued, push_fn not called


# U-SP19: dead pane on result submission → next task rolled back, push_fn not called


# U-SP20: _auto_enqueue_review inherits project from impl task context
def test_u_sp20_auto_enqueue_review_inherits_project(tmp_db):
    """When _auto_enqueue_review creates a review task for an impl task,
    it must copy context.project from the impl task into the review task context.
    This ensures cross-project routing can be detected."""
    push = RecordingPush()
    app = create_app(
        db_path=tmp_db,
        pane_map={"implementer": "%91", "reviewer": "%92"},
        push_fn=push,
        project="project_a",
    )
    with TestClient(app) as client:
        # Enqueue impl task with top-level project field
        impl = _task_payload("impl-sp20", project="project_a")
        impl["context"]["crew_run_branch"] = True
        client.post("/tasks", json=impl)

        # Submit completed result → triggers _auto_enqueue_review
        r = client.post("/tasks/impl-sp20/result", json=_result_payload("impl-sp20"))
        assert r.status_code == 200

    # The auto-created review task must carry project="project_a" as a top-level field
    from agent_crew.queue import TaskQueue
    q = TaskQueue(tmp_db)
    review_tasks = [t for t in q.list_tasks() if t.task_type == "review"]
    assert len(review_tasks) == 1
    assert review_tasks[0].project == "project_a"


# U-SP22: POST /gates/{id}/resolve approved → _try_push_next fires for all roles


# U-SP22b: gate approval with idle role and pending task → task dispatched




# U-SP23: POST /gates/{id}/resolve rejected → _try_push_next NOT called


# U-SP21: _auto_enqueue_review rejects cross-project impl tasks
def test_u_sp21_auto_enqueue_review_rejects_cross_project(tmp_db):
    """When the impl task's context.project doesn't match the server's project,
    _auto_enqueue_review must NOT enqueue a review task (skip silently).
    This prevents cross-project review routing."""
    push = RecordingPush()
    app = create_app(
        db_path=tmp_db,
        pane_map={"implementer": "%91", "reviewer": "%92"},
        push_fn=push,
        project="project_a",   # server is for project_a
    )
    with TestClient(app) as client:
        # Enqueue impl task with top-level project=project_b (different from server's project_a)
        impl = _task_payload("impl-sp21", project="project_b")
        client.post("/tasks", json=impl)

        # Submit result — should NOT auto-enqueue a review task
        r = client.post("/tasks/impl-sp21/result", json=_result_payload("impl-sp21"))
        assert r.status_code == 200

    from agent_crew.queue import TaskQueue
    q = TaskQueue(tmp_db)
    review_tasks = [t for t in q.list_tasks() if t.task_type == "review"]
    assert len(review_tasks) == 0, "cross-project review task must not be enqueued"
