"""#935: workers report results; the pipeline alone creates successors."""

import pytest
from fastapi.testclient import TestClient

from agent_crew.instructions import generate
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


SHA = "a" * 40


@pytest.mark.parametrize("role", ["implementer", "reviewer", "tester"])
def test_generated_role_protocol_never_tells_worker_to_enqueue_successor(role):
    content = generate(role, "agent_crew", 8105)
    assert "POST directly with the explicit type" not in content
    assert '"task_type": "review"' not in content
    assert '"task_type": "test"' not in content
    assert "return to polling" not in content


def _harness(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    db = str(tmp_path / "tasks.db")
    app = create_app(db, project="agent_crew", pane_map={},
                     identity_required=False, watchdog_disabled=True,
                     anomaly_disabled=True)
    return app, TaskQueue(db)


def _post(client, task_id="review-935", task_type="review", *, pr_number=None,
          context=None):
    payload = {"task_id": task_id, "task_type": task_type,
               "description": "Review the pinned change", "branch": "fix/935",
               "project": "agent_crew", "context": context or {}}
    if pr_number is not None:
        payload["pr_number"] = pr_number
    return client.post("/tasks", json=payload)


@pytest.mark.parametrize("task_type", ["review", "test"])
def test_post_accepts_context_pr_without_head_but_rejects_unpinned_pr(tmp_path, monkeypatch, task_type):
    app, queue = _harness(tmp_path, monkeypatch)
    with TestClient(app) as client:
        context_only = _post(client, task_type=task_type,
                             context={"pr_number": 935, "reviewed_sha": SHA})
        no_head = _post(client, task_id="no-head", task_type=task_type,
                        pr_number=936, context={"pr_number": 936})
        no_pr = _post(client, task_id="no-pr", task_type=task_type,
                      context={"reviewed_sha": SHA})
    assert context_only.status_code == 201, context_only.text
    assert no_head.status_code == 201, no_head.text
    assert no_pr.status_code == 422, no_pr.text
    assert queue.get_task("review-935").pr_number == 935
    assert queue.get_task("review-935").context["pr_number"] == 935
    assert queue.get_task("no-head").pr_number == 936
    assert queue.get_task("no-head").context["pr_number"] == 936
    assert queue.get_task("no-pr") is None


@pytest.mark.parametrize("task_type", ["review", "test"])
def test_pinned_post_stores_pr_column_and_refuses_duplicate_head(
        tmp_path, monkeypatch, task_type):
    app, queue = _harness(tmp_path, monkeypatch)
    queue.enqueue(TaskRequest(
        task_id="pipeline-935", task_type=task_type,
        description="Pipeline successor", branch="fix/935", project="agent_crew",
        context={"pr_number": 935, "reviewed_sha": SHA}))
    assert queue.get_task("pipeline-935").pr_number == 935
    with TestClient(app) as client:
        duplicate = _post(client, task_id="worker-935", task_type=task_type,
                          pr_number=935,
                          context={"pr_number": 935, "reviewed_sha": SHA})
    assert duplicate.status_code == 409, duplicate.text
    assert queue.get_task("worker-935") is None


def test_top_level_pr_is_canonicalized_for_context_consumers(tmp_path, monkeypatch):
    app, queue = _harness(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = _post(client, pr_number=935, context={"reviewed_sha": SHA})
        mismatch = _post(client, task_id="mismatch", pr_number=935,
                         context={"pr_number": 936, "reviewed_sha": SHA})
    assert response.status_code == 201, response.text
    assert mismatch.status_code == 422, mismatch.text
    task = queue.get_task("review-935")
    assert task.pr_number == 935
    assert task.context["pr_number"] == 935


def test_numeric_string_context_pr_matches_top_level(tmp_path, monkeypatch):
    app, queue = _harness(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = _post(client, pr_number="938",
                         context={"pr_number": "938", "reviewed_sha": SHA})
    assert response.status_code == 201, response.text
    assert queue.get_task("review-935").pr_number == 938
