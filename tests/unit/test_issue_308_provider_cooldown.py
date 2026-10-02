"""#308: provider exhaustion holds roles or records a skipped tester stage."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from agent_crew.cea.input_providers.budget import QuotaBudgetProvider
from agent_crew.pipeline import auto_enqueue_test, auto_fallback_failed_task
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


@pytest.fixture
def cooldown(tmp_path, monkeypatch):
    path = tmp_path / "cooldown.json"
    monkeypatch.setenv("AGENT_CREW_CEA_COOLDOWN_FILE", str(path))

    def write(provider, until):
        path.write_text(json.dumps({provider: until}))
        return path

    return write


@pytest.mark.parametrize("task_type,role,provider", [
    ("implement", "implementer", "codex"),
    ("review", "reviewer", "claude"),
])
def test_dispatch_holds_role_task_pending_during_cooldown(
        tmp_db, cooldown, monkeypatch, task_type, role, provider):
    until = time.time() + 3600
    cooldown(provider, until)
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    queue = TaskQueue(tmp_db)
    task_id = f"cooldown-{task_type}"
    queue.enqueue(TaskRequest(task_id=task_id, task_type=task_type,
                              description="work", branch="topic"))
    app = create_app(tmp_db, pane_map={}, watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        task = queue.dequeue(role=role)
        assert task is not None
        asyncio.run(app.state.dispatch_task(task, role))
    assert queue.get_task_status(task_id) == "pending"
    assert not any(t.task_id.startswith("fallback-") for t in queue.list_tasks())


def test_gemini_cooldown_skips_test_with_note(tmp_db, cooldown, monkeypatch):
    cooldown("gemini", time.time() + 3600)
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    queue = TaskQueue(tmp_db)
    review = "review-cooldown"
    queue.enqueue(TaskRequest(task_id=review, task_type="review",
                              description="review", branch="topic"))
    queue.submit_result(review, TaskResult(review, "completed", "approved",
                                          verdict="approve", findings=[]))
    assert auto_enqueue_test(queue, review) is None
    assert not any(t.task_type == "test" for t in queue.list_tasks())
    assert queue.get_task_context(review)["test_stage_skip"]["reason"] == (
        "gemini tester stage not run (provider limit)")

    # A test queued before cooldown began is also skipped before its worker runs.
    queued = "test-preexisting"
    queue.enqueue(TaskRequest(task_id=queued, task_type="test",
                              description="test", branch="topic",
                              context={"prev_task_id": review}))
    app = create_app(tmp_db, pane_map={}, watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        task = queue.dequeue(role="tester")
        assert task is not None
        asyncio.run(app.state.dispatch_task(task, "tester"))
    assert queue.get_task_status(queued) == "cancelled"
    assert queue.get_task_context(queued)["test_stage_skip"]["reason"] == (
        "gemini tester stage not run (provider limit)")


def test_cooldown_only_clears_on_file_clear_or_until(tmp_path):
    path = tmp_path / "cooldown.json"
    path.write_text(json.dumps({"codex": 200}))
    reader = QuotaBudgetProvider(cooldown_file=str(path), clock=lambda: 100)
    assert reader.active_cooldown_until("codex") == 200
    reader._clock = lambda: 201
    assert reader.active_cooldown_until("codex") is None
    reader._clock = lambda: 100
    path.write_text("{}")
    assert reader.active_cooldown_until("codex") is None


@pytest.mark.parametrize("body", ["{", "[1,2,3]", "42", '"codex"', "true", "null"])
def test_malformed_configured_cooldown_is_not_treated_as_clear(tmp_path, body):
    path = tmp_path / "cooldown.json"
    path.write_text(body)
    reader = QuotaBudgetProvider(cooldown_file=str(path), clock=lambda: 100)
    assert reader.active_cooldown_until("codex") == float("inf")


def test_task_post_with_wrong_shape_cooldown_holds_without_500(
        tmp_db, cooldown, monkeypatch):
    cooldown_file = cooldown("codex", time.time() + 3600)
    cooldown_file.write_text("[1,2,3]")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    app = create_app(tmp_db, pane_map={"implementer": "%100"},
                     push_fn=lambda *_: None, watchdog_disabled=True,
                     anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks", json={
            "task_id": "wrong-shape", "task_type": "implement",
            "description": "work", "branch": "topic", "priority": 3,
            "context": {}, "project": "",
        })
    assert response.status_code == 201
    assert TaskQueue(tmp_db).get_task_status("wrong-shape") == "pending"


def test_rate_limit_cannot_substitute_provider(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id="impl-limit", task_type="implement",
                              description="work", branch="topic"))
    result = TaskResult("impl-limit", "failed", "rate limit reached")
    assert auto_fallback_failed_task(queue, "impl-limit", result, "implement") is False
    assert not any(t.task_id.startswith("fallback-") for t in queue.list_tasks())
