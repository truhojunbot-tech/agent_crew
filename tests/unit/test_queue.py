import json
import sqlite3
import threading

import pytest

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


@pytest.fixture
def q(tmp_path):
    return TaskQueue(str(tmp_path / "test.db"))


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "shared.db")


def make_task(task_id="t-001", task_type="implement", priority=3):
    return TaskRequest(task_id=task_id, task_type=task_type, description="desc", priority=priority)


# U-Q01: Enqueue single task → status=pending
def test_u_q01_enqueue_returns_task_id(q):
    task = make_task()
    returned_id = q.enqueue(task)
    assert returned_id == task.task_id
    tasks = q.list_tasks(status="pending")
    assert len(tasks) == 1
    assert tasks[0].task_id == task.task_id


# U-Q02: Priority ordering — priority=1 task가 priority=3보다 먼저 dequeue
def test_u_q02_priority_ordering(q):
    q.enqueue(make_task("low", priority=3))
    q.enqueue(make_task("high", priority=1))
    first = q.dequeue()
    assert first is not None
    assert first.task_id == "high"


# U-Q03: Dequeue returns highest-priority pending task → status→in_progress
def test_u_q03_dequeue_changes_status(q):
    q.enqueue(make_task())
    result = q.dequeue()
    assert result is not None
    assert result.task_id == "t-001"
    pending = q.list_tasks(status="pending")
    assert len(pending) == 0
    in_progress = q.list_tasks(status="in_progress")
    assert len(in_progress) == 1


# U-Q04: Dequeue with role filter — task_type="review" 태스크만 반환
def test_u_q04_dequeue_role_filter(q):
    q.enqueue(make_task("impl", task_type="implement"))
    q.enqueue(make_task("rev", task_type="review"))
    result = q.dequeue(role="reviewer")
    assert result is not None
    assert result.task_id == "rev"
    assert result.task_type == "review"


# U-Q05: Dequeue empty queue → None
def test_u_q05_dequeue_empty_returns_none(q):
    result = q.dequeue()
    assert result is None


# U-Q06: Atomicity — 두 TaskQueue 인스턴스가 같은 DB에서 동시 dequeue 시 다른 task
def test_u_q06_atomic_dequeue(db_path):
    q1 = TaskQueue(db_path)
    q2 = TaskQueue(db_path)
    q1.enqueue(make_task("a", priority=1))
    q1.enqueue(make_task("b", priority=2))

    results = []
    errors = []

    def do_dequeue(queue):
        try:
            results.append(queue.dequeue())
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=do_dequeue, args=(q1,))
    t2 = threading.Thread(target=do_dequeue, args=(q2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors
    task_ids = {r.task_id for r in results if r is not None}
    assert len(task_ids) == 2


# U-Q07: Submit result — completed/failed/needs_human 모두 DB에 올바르게 저장되는지 검증
@pytest.mark.parametrize("result_status,verdict", [
    ("completed", "approve"),
    ("failed", None),
    ("needs_human", None),
])
def test_u_q07_submit_result(q, result_status, verdict):
    q.enqueue(make_task())
    q.dequeue()
    res = TaskResult(
        task_id="t-001",
        status=result_status,
        summary="Done",
        verdict=verdict,
        findings=["f1"],
        pr_number=7,
    )
    q.submit_result("t-001", res)

    # DB에서 직접 읽어서 모든 필드 검증
    conn = sqlite3.connect(q._db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tasks WHERE task_id = 't-001'").fetchone()
    conn.close()

    assert row["status"] == result_status
    assert row["summary"] == "Done"
    assert row["verdict"] == verdict
    assert json.loads(row["findings"]) == ["f1"]
    assert row["pr_number"] == 7


# U-Q08: Submit result for nonexistent task_id → ValueError
def test_u_q08_submit_result_nonexistent(q):
    res = TaskResult(task_id="ghost", status="completed", summary="nope")
    with pytest.raises(ValueError):
        q.submit_result("ghost", res)


# U-Q08b: Submit result where result.task_id != task_id argument → ValueError
def test_u_q08b_submit_result_task_id_mismatch(q):
    q.enqueue(make_task("t-001"))
    q.dequeue()
    res = TaskResult(task_id="other-id", status="completed", summary="mismatch")
    with pytest.raises(ValueError, match="task_id mismatch"):
        q.submit_result("t-001", res)


# U-Q09: Cancel task → status=cancelled, dequeue 시 반환 안 됨
def test_u_q09_cancel_task(q):
    q.enqueue(make_task())
    q.cancel("t-001")
    cancelled = q.list_tasks(status="cancelled")
    assert len(cancelled) == 1
    result = q.dequeue()
    assert result is None


# U-Q10: List tasks by status filter
def test_u_q10_list_tasks_by_status(q):
    q.enqueue(make_task("a"))
    q.enqueue(make_task("b"))
    q.cancel("a")
    pending = q.list_tasks(status="pending")
    assert len(pending) == 1
    assert pending[0].task_id == "b"
    all_tasks = q.list_tasks()
    assert len(all_tasks) == 2


# ---------------------------------------------------------------------------
# Gate tests (U-Q11 ~ U-Q17)
# ---------------------------------------------------------------------------

from agent_crew.protocol import GateRequest


def make_gate(gate_id="g-001", gate_type="approval", message="Please approve"):
    return GateRequest(id=gate_id, type=gate_type, message=message)


# U-Q11: Persistence — TaskQueue 재연결 후 기존 task 살아있는지
def test_u_q11_persistence(tmp_path):
    db = str(tmp_path / "persist.db")
    q1 = TaskQueue(db)
    q1.enqueue(make_task("t-persist"))
    q2 = TaskQueue(db)
    tasks = q2.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].task_id == "t-persist"


# U-Q12: Gate create → status=pending
def test_u_q12_gate_create(q):
    gate_id = q.create_gate(make_gate())
    assert gate_id == "g-001"
    gates = q.list_gates(status="pending")
    assert len(gates) == 1
    assert gates[0].id == "g-001"
    assert gates[0].status == "pending"


# U-Q13: Gate resolve approved → status=approved
def test_u_q13_gate_resolve_approved(q):
    q.create_gate(make_gate())
    q.resolve_gate("g-001", approved=True)
    gates = q.list_gates(status="approved")
    assert len(gates) == 1
    assert gates[0].status == "approved"


# U-Q14: Gate resolve rejected → status=rejected
def test_u_q14_gate_resolve_rejected(q):
    q.create_gate(make_gate())
    q.resolve_gate("g-001", approved=False)
    gates = q.list_gates(status="rejected")
    assert len(gates) == 1
    assert gates[0].status == "rejected"


# U-Q15: Gate list pending — pending 게이트만 반환
def test_u_q15_gate_list_pending(q):
    q.create_gate(make_gate("g-001"))
    q.create_gate(make_gate("g-002"))
    q.resolve_gate("g-001", approved=True)
    pending = q.list_gates(status="pending")
    assert len(pending) == 1
    assert pending[0].id == "g-002"


# U-Q16: Gate resolve nonexistent → ValueError
def test_u_q16_gate_resolve_nonexistent(q):
    with pytest.raises(ValueError):
        q.resolve_gate("ghost", approved=True)


# U-Q17: Gate resolve already resolved → ValueError (idempotency)
def test_u_q17_gate_resolve_already_resolved(q):
    q.create_gate(make_gate())
    q.resolve_gate("g-001", approved=True)
    with pytest.raises(ValueError):
        q.resolve_gate("g-001", approved=True)
