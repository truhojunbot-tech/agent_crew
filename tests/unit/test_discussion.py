from unittest.mock import MagicMock

from agent_crew.discussion import (
    DEFAULT_PERSPECTIVES,
    assign_perspectives,
    build_synthesis,
    enqueue_panel_tasks,
    multi_round,
    then_run,
)


# U-D01: enqueue_panel_tasks — agents 수만큼 task_type=discuss 태스크 등록됨
def test_u_d01_enqueue_panel_tasks():
    queue = MagicMock()
    queue.enqueue.side_effect = lambda req: req.task_id

    agents = ["claude", "codex", "gemini"]
    topic = "Should we adopt microservices?"
    context = {"repo": "agent_crew"}

    task_ids = enqueue_panel_tasks(queue, agents, topic, context)

    assert len(task_ids) == 3
    assert queue.enqueue.call_count == 3

    for c in queue.enqueue.call_args_list:
        req = c[0][0]
        assert req.task_type == "discuss"
        assert topic in req.description


# U-D02: assign_perspectives (default) — DEFAULT_PERSPECTIVES 라운드로빈 할당
def test_u_d02_assign_perspectives_default():
    agents = ["a", "b", "c", "d", "e"]
    result = assign_perspectives(agents)

    assert len(result) == 5
    for i, agent in enumerate(agents):
        assert result[agent] == DEFAULT_PERSPECTIVES[i % len(DEFAULT_PERSPECTIVES)]


# U-D03: assign_perspectives (custom) — 커스텀 perspectives 맵 적용
def test_u_d03_assign_perspectives_custom():
    agents = ["a", "b"]
    custom = ["tech", "business"]
    result = assign_perspectives(agents, perspectives=custom)

    assert result["a"] == "tech"
    assert result["b"] == "business"


# U-D04: build_synthesis — 모든 패널 의견 포함된 synthesis.md 문자열 생성
def test_u_d04_build_synthesis():
    results = [
        {"agent": "claude", "perspective": "analyst", "summary": "We need more data."},
        {"agent": "codex", "perspective": "critic", "summary": "This approach has risks."},
    ]
    synthesis = build_synthesis(results)

    assert "claude" in synthesis
    assert "codex" in synthesis
    assert "analyst" in synthesis
    assert "critic" in synthesis
    assert "We need more data." in synthesis
    assert "This approach has risks." in synthesis


# U-D05: multi_round — round 2 context에 round 1 synthesis 포함됨
def test_u_d05_multi_round():
    queue = MagicMock()
    enqueued_requests = []

    def capture_enqueue(req):
        enqueued_requests.append(req)
        return req.task_id

    queue.enqueue.side_effect = capture_enqueue

    agents = ["claude", "codex"]
    topic = "Architecture decision"

    final = multi_round(queue, agents, topic, rounds=2)

    assert isinstance(final, str)
    assert len(final) > 0

    round2_reqs = [r for r in enqueued_requests if r.context.get("round", 0) == 2]
    assert len(round2_reqs) > 0
    for req in round2_reqs:
        assert "prior_synthesis" in req.context


# U-D06: then_run — synthesis를 그대로 반환
def test_u_d06_then_run():
    synthesis = "## Panel Synthesis\n- Analyst: good\n- Critic: risky"
    result = then_run(synthesis)
    assert result == synthesis
