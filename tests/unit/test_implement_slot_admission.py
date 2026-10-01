"""Project-configured implement admission limit."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from agent_crew.queue import (
    AdmissionRefused, TaskQueue, _CEA_SYSTEM_SUCCESSOR_PROVENANCE,
)
from tests.unit.sev0_cea_acceptance_helpers import (
    AuthorityState, LiveState, inject_cea, receipt_by_id, task,
)


@pytest.fixture
def queue(tmp_path, monkeypatch):
    inject_cea(monkeypatch, LiveState(AuthorityState("active")))
    root = tmp_path / "crew"
    root.mkdir()
    state = root / "state.json"
    state.write_text(json.dumps({"project": "crew", "max_open_implement": 1}))
    return TaskQueue(str(root / "tasks.db")), state


def _add(q, task_id, task_type="implement", context=None):
    request = task(task_id, project="crew")
    request.task_type = task_type
    request.description = f"Distinct work {task_id}"
    request.context = context or {}
    return q.enqueue(request)


@pytest.mark.parametrize("status", ["pending", "in_progress"])
def test_open_implement_refused_with_block_receipt(queue, status):
    q, _ = queue
    _add(q, "first")
    with sqlite3.connect(q._db_path) as conn:
        conn.execute("UPDATE tasks SET status=? WHERE task_id='first'", (status,))
    with pytest.raises(AdmissionRefused) as exc:
        _add(q, "second")
    receipt = receipt_by_id(q, exc.value.receipt_id)
    assert receipt["decision"] == "BLOCK"
    assert receipt["reason"]["code"] == "IMPLEMENT_SLOT_BUSY"
    assert "first" in receipt["reason"]["text"]
    assert q.get_task_status("second") is None


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_terminal_implement_frees_slot(queue, status):
    q, _ = queue
    _add(q, "first")
    with sqlite3.connect(q._db_path) as conn:
        conn.execute("UPDATE tasks SET status=? WHERE task_id='first'", (status,))
    _add(q, "second")
    assert q.get_task_status("second") == "pending"


def test_absent_cap_allows_parallel_implement(queue):
    q, state = queue
    _add(q, "first")
    state.write_text(json.dumps({"project": "crew"}))
    _add(q, "second")
    assert q.get_task_status("second") == "pending"


@pytest.mark.parametrize("task_type", ["review", "test"])
def test_other_types_are_unaffected(queue, task_type):
    q, _ = queue
    _add(q, "first")
    _add(q, "second", task_type)
    assert q.get_task_status("second") == "pending"


def test_override_is_admitted_and_audited(queue):
    q, _ = queue
    _add(q, "first")
    _add(q, "second", context={"allow_parallel_implement": True})
    assert q.get_task_status("second") == "pending"
    with sqlite3.connect(q._db_path) as conn:
        assert conn.execute("SELECT 1 FROM task_exec_events WHERE task_id='second' "
                            "AND event='parallel_implement_override'").fetchone()


def test_system_fix_successor_is_exempt(queue):
    q, _ = queue
    _add(q, "parent")
    with sqlite3.connect(q._db_path) as conn:
        conn.execute("UPDATE tasks SET status='failed' WHERE task_id='parent'")
    _add(q, "first")
    request = task("fix-parent-r1", project="crew")
    request.task_type = "implement"
    request.description = "Fix review finding"
    request.context = {"prev_task_id": "parent", "fix_round": 1}
    q.enqueue(request, ingress="cascade.fix",
              _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
    assert q.get_task_status(request.task_id) == "pending"


def test_caller_cannot_claim_fix_exemption(queue):
    q, _ = queue
    _add(q, "first")
    request = task("claimed-fix", project="crew")
    request.task_type = "implement"
    request.context = {"prev_task_id": "first", "fix_round": 1}
    with pytest.raises(AdmissionRefused) as exc:
        q.enqueue(request, ingress="cascade.fix")
    assert receipt_by_id(q, exc.value.receipt_id)["reason"]["code"] == "IMPLEMENT_SLOT_BUSY"


def test_concurrent_admissions_use_one_slot(queue):
    q, _ = queue
    barrier = Barrier(2)

    def submit(task_id):
        barrier.wait()
        try:
            _add(q, task_id)
            return "admitted"
        except AdmissionRefused as exc:
            return receipt_by_id(q, exc.receipt_id)["reason"]["code"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ("first", "second")))
    assert sorted(results) == ["IMPLEMENT_SLOT_BUSY", "admitted"]
