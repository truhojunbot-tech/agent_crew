"""#649: root risk is mandatory and the declaration follows its lineage."""

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from agent_crew.cli import crew
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.queue import _CEA_SYSTEM_SUCCESSOR_PROVENANCE
from agent_crew.pipeline import _enqueue_with_inherited_risk
from agent_crew.server import create_app


@pytest.mark.parametrize("task_type", ["implement", "review", "test", "discuss"])
def test_http_root_requires_risk_tier(tmp_path, task_type):
    db = str(tmp_path / "tasks.db")
    app = create_app(db, pane_map={}, project="agent_crew",
                     push_fn=lambda *a, **k: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks", json={
            "task_id": f"root-{task_type}", "task_type": task_type,
            "description": "work", "context": {}},
            headers={"X-Agent-Crew-Project": "agent_crew"})
    assert response.status_code == 422
    assert "--risk-tier" in response.text
    assert TaskQueue(db).get_task(f"root-{task_type}") is None


def test_run_without_risk_tier_fails_before_enqueue(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_DEFAULT_RISK_TIER", raising=False)
    db = str(tmp_path / "tasks.db")
    result = CliRunner().invoke(crew, ["run", "work", "--db", db])
    assert result.exit_code != 0
    assert "--risk-tier" in result.output
    assert not (tmp_path / "tasks.db").exists()


def test_cascade_chain_keeps_root_tier_and_declaration(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("impl-root", "implement", "routine work",
                              context={"risk_tier": 2}), ingress="loop.implement")
    root = queue.get_task("impl-root")
    declaration = root.context["risk_declaration"]
    previous = root
    for task_id, task_type, ingress in (
            ("review-root", "review", "cascade.review"),
            ("fix-review-root", "implement", "cascade.fix"),
            ("retry-fix-root", "implement", "retry.failed_task"),
            ("test-root", "test", "cascade.test")):
        request = TaskRequest(task_id, task_type, "routine work", context={
            "prev_task_id": previous.task_id})
        _enqueue_with_inherited_risk(
            queue, request, {task.task_id: task for task in queue.list_tasks()},
            previous, ingress=ingress,
            successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
        previous = queue.get_task(task_id)
        assert previous.context["risk_tier"] == 2
        assert previous.context["risk_declaration"] == declaration


def test_http_successor_inherits_root_risk(tmp_path):
    db = str(tmp_path / "tasks.db")
    app = create_app(db, pane_map={}, project="agent_crew",
                     push_fn=lambda *a, **k: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    headers = {"X-Agent-Crew-Project": "agent_crew"}
    with TestClient(app) as client:
        root = client.post("/tasks", json={
            "task_id": "impl-root", "task_type": "implement", "description": "work",
            "context": {"risk_tier": 2}}, headers=headers)
        assert root.status_code == 201, root.text
        child = client.post("/tasks", json={
            "task_id": "review-root", "task_type": "review", "description": "review",
            "context": {"prev_task_id": "impl-root"}}, headers=headers)
    assert child.status_code == 201, child.text
    queue = TaskQueue(db)
    root_context = queue.get_task_context("impl-root")
    child_context = queue.get_task_context("review-root")
    assert child_context["risk_tier"] == 2
    assert child_context["risk_declaration"] == root_context["risk_declaration"]


@pytest.mark.parametrize("command", [
    ["discuss", "topic", "--db"],
    ["enqueue", "review", "work", "--db"],
    ["enqueue", "test", "work", "--db"],
])
def test_other_cli_roots_require_tier(tmp_path, monkeypatch, command):
    monkeypatch.delenv("AGENT_CREW_DEFAULT_RISK_TIER", raising=False)
    result = CliRunner().invoke(crew, [*command, str(tmp_path / "tasks.db")])
    assert result.exit_code != 0
    assert "--risk-tier" in result.output
