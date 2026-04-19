"""
Triage + gate integration tests.

Uses task_queue (direct TaskQueue) for gate/task operations,
and test_client (HTTP) for gate resolution and state verification.
Both share the same tmp_db.

gh CLI subprocess is monkeypatched via fetch_issues_from_gh.
Triage agent is injected as a callable (agent_fn).
"""

import pytest

import agent_crew.triage as triage
from agent_crew.triage import check_gate_timeout, enqueue_task, run


pytestmark = pytest.mark.integration

_REPO = "org/agent_crew"

# Raw gh CLI JSON output (same shape as `gh issue list --json number,title,labels`)
_GH_ISSUES = [
    {"number": 7, "title": "Add login feature", "labels": [{"name": "enhancement"}]},
    {"number": 8, "title": "Fix null pointer", "labels": [{"name": "bug"}]},
    {"number": 9, "title": "Old done issue", "labels": [{"name": "agent_crew:done"}]},
]

# All issues already processed
_GH_ISSUES_ALL_DONE = [
    {"number": 9, "title": "Old done issue", "labels": [{"name": "agent_crew:done"}]},
]

_AGENT_RESPONSE = "ISSUE: 7\nDESCRIPTION: Implement user login feature"


def _mock_gh(issues):
    """Return a monkeypatch target that replaces fetch_issues_from_gh."""
    return lambda repo: issues


def _agent_fn(response_text):
    return lambda prompt: response_text


# I-TR01: gh mock → triage.run() creates gate → HTTP approve → enqueue_task → task in queue
def test_i_tr01_triage_approve_enqueues_task(monkeypatch, task_queue, test_client):
    monkeypatch.setattr(triage, "fetch_issues_from_gh", _mock_gh(_GH_ISSUES))

    result = run(task_queue, _REPO, _agent_fn(_AGENT_RESPONSE))
    assert result is not None
    gate_id = result["gate_id"]
    assert result["parsed"] == {"issue": 7, "description": "Implement user login feature"}

    # gate가 pending 상태로 서버에 등록됐는지 확인
    resp = test_client.get("/gates/pending")
    pending_ids = {g["id"] for g in resp.json()}
    assert gate_id in pending_ids

    # HTTP로 gate 승인
    resp = test_client.post(f"/gates/{gate_id}/resolve", json={"status": "approved"})
    assert resp.status_code == 200

    # 승인 후 task enqueue
    task_id = enqueue_task(task_queue, result)

    resp = test_client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    task = resp.json()
    assert task["task_type"] == "implement"
    assert task["context"]["issue"] == 7

    # gate는 pending에서 제거됨
    resp = test_client.get("/gates/pending")
    assert gate_id not in {g["id"] for g in resp.json()}


# I-TR02: gh mock → triage.run() creates gate → HTTP reject → no task enqueued
def test_i_tr02_triage_reject_no_task(monkeypatch, task_queue, test_client):
    monkeypatch.setattr(triage, "fetch_issues_from_gh", _mock_gh(_GH_ISSUES))

    result = run(task_queue, _REPO, _agent_fn(_AGENT_RESPONSE))
    assert result is not None
    gate_id = result["gate_id"]

    # HTTP로 gate 거부
    resp = test_client.post(f"/gates/{gate_id}/resolve", json={"status": "rejected"})
    assert resp.status_code == 200

    # gate 거부 후 task enqueue 안 함 — pending에 implement 없음
    resp = test_client.get("/tasks", params={"status": "pending"})
    assert not any(t["task_type"] == "implement" for t in resp.json())

    # gate 상태 확인
    resp = test_client.get(f"/gates/{gate_id}")
    assert resp.json()["status"] == "rejected"


# I-TR03: all issues done → gh mock returns no open issues → run() returns None, no gate
def test_i_tr03_no_issues_clean_exit(monkeypatch, task_queue, test_client):
    monkeypatch.setattr(triage, "fetch_issues_from_gh", _mock_gh(_GH_ISSUES_ALL_DONE))

    result = run(task_queue, _REPO, _agent_fn(_AGENT_RESPONSE))
    assert result is None

    # 게이트/태스크가 전혀 생성되지 않음
    resp = test_client.get("/gates/pending")
    assert resp.json() == []

    resp = test_client.get("/tasks", params={"status": "pending"})
    assert resp.json() == []


# I-TR04: gate timeout → check_gate_timeout() auto-rejects → next cycle proceeds
def test_i_tr04_gate_timeout_auto_reject(monkeypatch, task_queue, test_client):
    monkeypatch.setattr(triage, "fetch_issues_from_gh", _mock_gh(_GH_ISSUES))

    # 첫 번째 사이클: gate 생성
    result = run(task_queue, _REPO, _agent_fn(_AGENT_RESPONSE))
    assert result is not None
    gate_id = result["gate_id"]

    resp = test_client.get("/gates/pending")
    assert gate_id in {g["id"] for g in resp.json()}

    # timeout 경과 시뮬레이션: timeout_seconds=0 → 생성 즉시 expired
    rejected = check_gate_timeout(task_queue, timeout_seconds=0)
    assert gate_id in rejected

    # gate가 auto-rejected됐는지 확인
    resp = test_client.get(f"/gates/{gate_id}")
    assert resp.json()["status"] == "rejected"

    # pending에서 제거됨
    resp = test_client.get("/gates/pending")
    assert gate_id not in {g["id"] for g in resp.json()}

    # 다음 사이클: 새로 run() → approve → enqueue 정상 동작
    result2 = run(task_queue, _REPO, _agent_fn(_AGENT_RESPONSE))
    assert result2 is not None
    gate_id2 = result2["gate_id"]

    resp = test_client.post(f"/gates/{gate_id2}/resolve", json={"status": "approved"})
    assert resp.status_code == 200

    task_id = enqueue_task(task_queue, result2)
    resp = test_client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["task_type"] == "implement"
