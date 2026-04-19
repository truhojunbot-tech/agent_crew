import pytest
from fastapi.testclient import TestClient

from agent_crew.discussion import (
    assign_perspectives,
    build_synthesis,
    enqueue_panel_tasks,
    multi_round,
)
from agent_crew.server import create_app


pytestmark = pytest.mark.integration


class _HTTPQueueAdapter:
    """Wraps TestClient so enqueue_panel_tasks can POST tasks via HTTP."""

    def __init__(self, client: TestClient):
        self._client = client
        self.enqueued: list[str] = []

    def enqueue(self, task) -> str:
        resp = self._client.post("/tasks", json={
            "task_id": task.task_id,
            "task_type": task.task_type,
            "description": task.description,
            "branch": task.branch,
            "priority": task.priority,
            "context": task.context,
        })
        assert resp.status_code == 201
        task_id = resp.json()["task_id"]
        self.enqueued.append(task_id)
        return task_id


def _submit_result(client: TestClient, task_id: str, agent: str, perspective: str,
                   summary: str, status: str = "completed") -> dict:
    client.post(f"/tasks/{task_id}/result", json={
        "task_id": task_id,
        "status": status,
        "summary": summary,
        "findings": [],
    })
    return {"agent": agent, "perspective": perspective, "summary": summary}


# I-DI01: 단일 라운드 discussion — tasks enqueued, mock 결과 제출, synthesis 생성
def test_i_di01_single_round(tmp_db):
    agents = ["claude", "codex", "gemini"]
    topic = "Should we adopt microservices?"

    with TestClient(create_app(tmp_db)) as client:
        queue = _HTTPQueueAdapter(client)
        task_ids = enqueue_panel_tasks(queue, agents, topic, context={})
        assert len(task_ids) == 3

        perspectives = assign_perspectives(agents)
        results = []
        for task_id, agent in zip(task_ids, agents):
            summary = f"{agent} opinion on {topic}"
            r = _submit_result(client, task_id, agent, perspectives[agent], summary)
            results.append(r)

        synthesis = build_synthesis(results, topic=topic)

    assert "## Topic" in synthesis
    assert "## Panel Opinions" in synthesis
    assert topic in synthesis
    for agent in agents:
        assert agent in synthesis


# I-DI02: multi_round(rounds=2) — round 2 tasks context에 round 1 synthesis 포함
def test_i_di02_multi_round_context(tmp_db):
    agents = ["claude", "codex"]
    topic = "Architecture decision"

    with TestClient(create_app(tmp_db)) as client:
        queue = _HTTPQueueAdapter(client)
        multi_round(queue, agents, topic, rounds=2)

        # round=2인 task 중 prior_synthesis context 확인
        resp = client.get("/tasks", params={"status": "pending"})
        all_tasks = resp.json()

    round2_tasks = [t for t in all_tasks if t["context"].get("round") == 2]
    assert len(round2_tasks) > 0
    for task in round2_tasks:
        assert "prior_synthesis" in task["context"]


# I-DI03: 2 agents로 discussion — 3명 미만에서도 동작
def test_i_di03_two_agents(tmp_db):
    agents = ["claude", "codex"]
    topic = "Monolith vs microservices"

    with TestClient(create_app(tmp_db)) as client:
        queue = _HTTPQueueAdapter(client)
        task_ids = enqueue_panel_tasks(queue, agents, topic, context={})
        assert len(task_ids) == 2

        perspectives = assign_perspectives(agents)
        results = []
        for task_id, agent in zip(task_ids, agents):
            r = _submit_result(client, task_id, agent, perspectives[agent], f"{agent} says yes")
            results.append(r)

        synthesis = build_synthesis(results, topic=topic)

    assert "claude" in synthesis
    assert "codex" in synthesis
    assert "## Panel Opinions" in synthesis


# I-DI04: agent 1명 실패 — 실패 기록, synthesis에 누락 의견 표시
def test_i_di04_agent_failure(tmp_db):
    agents = ["claude", "codex", "gemini"]
    topic = "Database choice"

    with TestClient(create_app(tmp_db)) as client:
        queue = _HTTPQueueAdapter(client)
        task_ids = enqueue_panel_tasks(queue, agents, topic, context={})

        perspectives = assign_perspectives(agents)
        results = []

        # claude, codex: 성공
        for task_id, agent in zip(task_ids[:2], agents[:2]):
            r = _submit_result(client, task_id, agent, perspectives[agent], f"{agent} opinion")
            results.append(r)

        # gemini: 실패
        failed_task_id = task_ids[2]
        client.post(f"/tasks/{failed_task_id}/result", json={
            "task_id": failed_task_id,
            "status": "failed",
            "summary": "Agent timed out",
            "findings": [],
        })
        results.append({
            "agent": "gemini",
            "perspective": perspectives["gemini"],
            "summary": "[FAILED] Agent timed out — opinion missing",
        })

        synthesis = build_synthesis(results, topic=topic)

    assert "claude" in synthesis
    assert "codex" in synthesis
    assert "gemini" in synthesis
    assert "FAILED" in synthesis or "missing" in synthesis
