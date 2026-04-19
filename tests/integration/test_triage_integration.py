"""
Triage + gate integration tests.

Uses task_queue (direct TaskQueue) for gate/task operations,
and test_client (HTTP) for gate resolution and state verification.
Both share the same tmp_db.
"""

import pytest

from agent_crew.protocol import GateRequest, TaskRequest
from agent_crew.triage import build_prompt, filter_processed, parse_issues, parse_response


pytestmark = pytest.mark.integration

_RAW_ISSUES = [
    {"number": 7, "title": "Add login feature", "labels": [{"name": "enhancement"}]},
    {"number": 8, "title": "Fix null pointer", "labels": [{"name": "bug"}]},
    {"number": 9, "title": "Old done issue", "labels": [{"name": "agent_crew:done"}]},
]

_TRIAGE_RESPONSE = "ISSUE: 7\nDESCRIPTION: Implement user login feature"


def _create_triage_gate(task_queue, issue_number: int) -> str:
    gate = GateRequest(
        id=f"gate-triage-{issue_number}",
        type="approval",
        message=f"Triage selected issue #{issue_number} — approve to enqueue?",
    )
    return task_queue.create_gate(gate)


def _enqueue_from_triage(task_queue, parsed: dict, branch: str = "main") -> str:
    req = TaskRequest(
        task_id=f"impl-triage-{parsed['issue']}",
        task_type="implement",
        description=parsed["description"],
        branch=branch,
        context={"issue": parsed["issue"]},
    )
    return task_queue.enqueue(req)


# I-TR01: Triage → gate created → approved → task enqueued
def test_i_tr01_triage_approve_enqueues_task(task_queue, test_client):
    issues = parse_issues(_RAW_ISSUES)
    filtered = filter_processed(issues)
    assert len(filtered) == 2  # issue #9 (agent_crew:done) filtered out

    prompt = build_prompt(filtered, merge_history="none")
    assert prompt is not None

    parsed = parse_response(_TRIAGE_RESPONSE)
    assert parsed == {"issue": 7, "description": "Implement user login feature"}

    gate_id = _create_triage_gate(task_queue, parsed["issue"])

    # gate가 pending 상태로 생성됐는지 HTTP로 확인
    resp = test_client.get("/gates/pending")
    pending_ids = {g["id"] for g in resp.json()}
    assert gate_id in pending_ids

    # gate 승인
    resp = test_client.post(f"/gates/{gate_id}/resolve", json={"status": "approved"})
    assert resp.status_code == 200

    # 승인됐으면 task enqueue
    task_id = _enqueue_from_triage(task_queue, parsed)

    resp = test_client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    task = resp.json()
    assert task["task_type"] == "implement"
    assert task["context"]["issue"] == 7

    # gate는 더 이상 pending 아님
    resp = test_client.get("/gates/pending")
    pending_ids = {g["id"] for g in resp.json()}
    assert gate_id not in pending_ids


# I-TR02: Triage → gate rejected → no task enqueued
def test_i_tr02_triage_reject_no_task(task_queue, test_client):
    issues = parse_issues(_RAW_ISSUES)
    filter_processed(issues)
    parsed = parse_response(_TRIAGE_RESPONSE)

    gate_id = _create_triage_gate(task_queue, parsed["issue"])

    # gate 거부
    resp = test_client.post(f"/gates/{gate_id}/resolve", json={"status": "rejected"})
    assert resp.status_code == 200

    # 거부됐으면 task enqueue 안 함 — pending tasks에 implement 없음
    resp = test_client.get("/tasks", params={"status": "pending"})
    pending = resp.json()
    assert not any(t["task_type"] == "implement" for t in pending)

    # gate는 rejected 상태
    resp = test_client.get(f"/gates/{gate_id}")
    assert resp.json()["status"] == "rejected"


# I-TR03: No open issues → clean exit, no gate created
def test_i_tr03_no_issues_clean_exit(task_queue, test_client):
    # 모든 이슈가 done 처리된 경우
    all_done = [
        {"number": 1, "title": "Done task", "labels": [{"name": "agent_crew:done"}]},
    ]
    issues = parse_issues(all_done)
    filtered = filter_processed(issues)
    assert filtered == []

    # build_prompt가 None 반환 → 게이트/태스크 생성 안 함
    prompt = build_prompt(filtered, merge_history="none")
    assert prompt is None

    # 서버에 아무것도 생성되지 않음
    resp = test_client.get("/gates/pending")
    assert resp.json() == []

    resp = test_client.get("/tasks", params={"status": "pending"})
    assert resp.json() == []


# I-TR04: Gate timeout → auto-reject, next cycle proceeds
def test_i_tr04_gate_timeout_auto_reject(task_queue, test_client):
    issues = parse_issues(_RAW_ISSUES)
    filter_processed(issues)
    parsed = parse_response(_TRIAGE_RESPONSE)

    # 첫 번째 사이클: gate 생성 후 timeout → auto-reject
    gate_id = _create_triage_gate(task_queue, parsed["issue"])

    resp = test_client.get("/gates/pending")
    assert gate_id in {g["id"] for g in resp.json()}

    # timeout 시뮬레이션: auto-reject
    task_queue.resolve_gate(gate_id, approved=False)

    resp = test_client.get(f"/gates/{gate_id}")
    assert resp.json()["status"] == "rejected"

    # pending에서 제거됨
    resp = test_client.get("/gates/pending")
    assert gate_id not in {g["id"] for g in resp.json()}

    # 다음 사이클은 정상 진행 가능 — 새 gate 생성 + 승인 + task enqueue
    gate_id2 = f"gate-triage-{parsed['issue']}-cycle2"
    gate2 = GateRequest(id=gate_id2, type="approval",
                        message=f"Retry: issue #{parsed['issue']}")
    task_queue.create_gate(gate2)

    resp = test_client.post(f"/gates/{gate_id2}/resolve", json={"status": "approved"})
    assert resp.status_code == 200

    req = TaskRequest(
        task_id=f"impl-triage-{parsed['issue']}-retry",
        task_type="implement",
        description=parsed["description"],
        branch="main",
        context={"issue": parsed["issue"], "retry": True},
    )
    task_id = task_queue.enqueue(req)

    resp = test_client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["context"]["retry"] is True
