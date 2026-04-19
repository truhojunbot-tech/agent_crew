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


# I-DI02: multi_round(rounds=2) — round 2 tasks context에 round 1 synthesis 내용 포함
def test_i_di02_multi_round_context(tmp_db):
    agents = ["claude", "codex"]
    topic = "Architecture decision"

    with TestClient(create_app(tmp_db)) as client:
        queue = _HTTPQueueAdapter(client)
        multi_round(queue, agents, topic, rounds=2)

        resp = client.get("/tasks", params={"status": "pending"})
        all_tasks = resp.json()

    round2_tasks = [t for t in all_tasks if t["context"].get("round") == 2]
    assert len(round2_tasks) > 0
    for task in round2_tasks:
        prior = task["context"].get("prior_synthesis", "")
        assert prior, "prior_synthesis must be non-empty"
        # round 1 synthesis는 topic과 agent 이름을 포함해야 함
        assert topic in prior or "## Panel Opinions" in prior or "claude" in prior


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


# I-DI04: agent 1명 실패 — 서버 confirmed status로 synthesis entry 도출, 실패 표시 검증
def test_i_di04_agent_failure(tmp_db):
    agents = ["claude", "codex", "gemini"]
    topic = "Database choice"

    with TestClient(create_app(tmp_db)) as client:
        queue = _HTTPQueueAdapter(client)
        task_ids = enqueue_panel_tasks(queue, agents, topic, context={})
        perspectives = assign_perspectives(agents)

        # claude, codex: 성공 결과 제출
        success_summaries = {}
        for task_id, agent in zip(task_ids[:2], agents[:2]):
            summary = f"{agent} opinion on {topic}"
            client.post(f"/tasks/{task_id}/result", json={
                "task_id": task_id,
                "status": "completed",
                "summary": summary,
                "findings": [],
            })
            success_summaries[task_id] = summary

        # gemini: 실패 결과 제출 (실제 HTTP POST)
        failed_task_id = task_ids[2]
        client.post(f"/tasks/{failed_task_id}/result", json={
            "task_id": failed_task_id,
            "status": "failed",
            "summary": "Agent timed out",
            "findings": [],
        })

        # 서버에서 실제 task status 쿼리 — failed task_id 집합 확인
        failed_resp = client.get("/tasks", params={"status": "failed"})
        failed_ids = {t["task_id"] for t in failed_resp.json()}
        assert failed_task_id in failed_ids, "failed task must be recorded on server"

        # 서버 확인 status 기반으로 synthesis results 구성
        results = []
        for task_id, agent in zip(task_ids, agents):
            perspective = perspectives[agent]
            if task_id in failed_ids:
                summary = f"[FAILED] {agent} did not respond — opinion missing"
            else:
                summary = success_summaries[task_id]
            results.append({"agent": agent, "perspective": perspective, "summary": summary})

        synthesis = build_synthesis(results, topic=topic)

    assert "claude" in synthesis
    assert "codex" in synthesis
    assert "gemini" in synthesis
    assert "FAILED" in synthesis
