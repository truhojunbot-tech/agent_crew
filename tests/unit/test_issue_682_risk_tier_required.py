"""#682: root risk is required before admission writes a task row."""

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue, _CEA_SYSTEM_SUCCESSOR_PROVENANCE
from agent_crew.server import create_app


@pytest.mark.parametrize("task_type", ["implement", "review", "test", "discuss"])
@pytest.mark.parametrize("context", [{}, {"prev_task_id": "forged"},
                                     {"risk_declaration": {"declaration_source": "explicit"}}])
def test_http_root_without_tier_is_422_and_not_inserted(tmp_path, task_type, context):
    db = str(tmp_path / "tasks.db")
    app = create_app(db, pane_map={}, project="agent_crew", push_fn=lambda *a, **k: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks", json={"task_id": "root", "task_type": task_type,
                       "description": "work", "context": context},
                       headers={"X-Agent-Crew-Project": "agent_crew"})
    assert response.status_code == 422
    assert "context.risk_tier" in response.text and "0-3" in response.text
    assert TaskQueue(db).get_task("root") is None


@pytest.mark.parametrize("tier", [True, "2", 2.0, -1, 4])
def test_http_invalid_tier_is_422_and_not_inserted(tmp_path, tier):
    db = str(tmp_path / "tasks.db")
    app = create_app(db, pane_map={}, project="agent_crew", push_fn=lambda *a, **k: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks", json={"task_id": "root", "task_type": "review",
                       "description": "work", "context": {"risk_tier": tier}},
                       headers={"X-Agent-Crew-Project": "agent_crew"})
    assert response.status_code == 422
    assert TaskQueue(db).get_task("root") is None


@pytest.mark.parametrize("tier", range(4))
def test_http_valid_tier_is_accepted(tmp_path, tier):
    db = str(tmp_path / "tasks.db")
    app = create_app(db, pane_map={}, project="agent_crew", push_fn=lambda *a, **k: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks", json={"task_id": "root", "task_type": "test",
                       "description": "work", "context": {"risk_tier": tier}},
                       headers={"X-Agent-Crew-Project": "agent_crew"})
    assert response.status_code == 201, response.text
    assert TaskQueue(db).get_task("root") is not None


def test_trusted_generated_successor_without_tier_is_accepted(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("root", "implement", "work", context={"risk_tier": 1}))
    queue.enqueue(TaskRequest("review-root", "review", "work",
                              context={"prev_task_id": "root"}),
                  _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
    assert queue.get_task("review-root") is not None
