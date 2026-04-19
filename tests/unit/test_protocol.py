import pytest
from agent_crew.protocol import GateRequest, TaskRequest, TaskResult


def test_u_p01_task_request_valid_construction():
    req = TaskRequest(
        task_id="t-001",
        task_type="implement",
        description="Build the feature",
        branch="feat/foo",
        priority=1,
        context={"key": "value"},
    )
    assert req.task_id == "t-001"
    assert req.task_type == "implement"
    assert req.description == "Build the feature"
    assert req.branch == "feat/foo"
    assert req.priority == 1
    assert req.context == {"key": "value"}

    # defaults
    req2 = TaskRequest(task_id="t-002", task_type="review", description="Review PR")
    assert req2.branch == ""
    assert req2.priority == 3
    assert req2.context == {}


def test_u_p02_task_request_rejects_invalid_type():
    with pytest.raises(ValueError):
        TaskRequest(task_id="t-003", task_type="invalid", description="bad")


def test_u_p03_task_result_valid_construction():
    result = TaskResult(
        task_id="t-001",
        status="completed",
        summary="All good",
        verdict="approve",
        findings=["finding1", "finding2"],
        pr_number=42,
    )
    assert result.task_id == "t-001"
    assert result.status == "completed"
    assert result.summary == "All good"
    assert result.verdict == "approve"
    assert result.findings == ["finding1", "finding2"]
    assert result.pr_number == 42

    # defaults
    result2 = TaskResult(task_id="t-002", status="failed", summary="Broke")
    assert result2.verdict is None
    assert result2.findings == []
    assert result2.pr_number is None


def test_u_p04_task_result_rejects_invalid_status():
    with pytest.raises(ValueError):
        TaskResult(task_id="t-003", status="unknown", summary="bad")


def test_u_p05_gate_request_valid_construction():
    gate = GateRequest(id="g-001", type="approval", message="Please approve")
    assert gate.id == "g-001"
    assert gate.type == "approval"
    assert gate.message == "Please approve"
    assert gate.status == "pending"
    assert isinstance(gate.created_at, float)
    assert gate.created_at > 0


def test_u_p06_gate_request_rejects_invalid_type():
    with pytest.raises(ValueError):
        GateRequest(id="g-002", type="invalid", message="bad")


def test_u_p07_task_request_rejects_priority_zero():
    with pytest.raises(ValueError):
        TaskRequest(task_id="t-007", task_type="implement", description="bad", priority=0)


def test_u_p08_task_request_rejects_priority_six():
    with pytest.raises(ValueError):
        TaskRequest(task_id="t-008", task_type="implement", description="bad", priority=6)


def test_u_p09_task_request_accepts_boundary_priorities():
    req_low = TaskRequest(task_id="t-009a", task_type="implement", description="low", priority=1)
    assert req_low.priority == 1
    req_high = TaskRequest(task_id="t-009b", task_type="implement", description="high", priority=5)
    assert req_high.priority == 5
