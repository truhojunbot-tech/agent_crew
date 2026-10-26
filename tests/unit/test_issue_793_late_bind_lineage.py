"""#793: late intent binds adopt the completed pipeline that followed a task."""

import json
import sqlite3

import pytest

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import _CEA_SYSTEM_SUCCESSOR_PROVENANCE, TaskQueue


OP_ID = "alfred44:late-bind"
RECEIVED_AT = 2_000.0


@pytest.fixture
def queue(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.admit_work_intent({
        "external_operation_id": OP_ID, "project": "alfred",
        "repo": "owner/alfred", "issue": 44,
        "target_repo": "owner/alfred", "target_issue": 405,
        "coordinator_identity": "alfred-owner",
        "coordinator_session": "session-1", "priority": 1,
        "risk_tier": 3, "execution_slo_s": 60,
    })
    with sqlite3.connect(queue.db_path) as conn:
        conn.execute("UPDATE work_intents SET received_at=? WHERE external_operation_id=?",
                     (RECEIVED_AT, OP_ID))
    return queue


def _task(queue, task_id, role, *, parent=None, status="completed",
          created_at=RECEIVED_AT + 10, verdict=None, operation_id=None,
          retry_of=None):
    context = {"issue": 405, "repo": "owner/alfred", "risk_tier": 2}
    if parent is not None:
        context["prev_task_id"] = parent
    if operation_id is not None:
        context["external_operation_id"] = operation_id
    if retry_of is not None:
        context["original_task_id"] = retry_of
    queue.enqueue(TaskRequest(task_id=task_id, task_type=role,
                              description=f"{role} for #405", branch="agent/405",
                              project="alfred", context=context),
                  ingress="retry.failed_task" if retry_of else "http.tasks",
                  _successor_provenance=(
                      _CEA_SYSTEM_SUCCESSOR_PROVENANCE if retry_of else None))
    with sqlite3.connect(queue.db_path) as conn:
        conn.execute("UPDATE tasks SET status=?, created_at=?, verdict=? WHERE task_id=?",
                     (status, created_at, verdict, task_id))


def _pipeline(queue, *, test_status="completed"):
    _task(queue, "impl-405", "implement")
    _task(queue, "review-1", "review", parent="impl-405",
          verdict="request_changes", created_at=RECEIVED_AT + 20)
    _task(queue, "fix-1", "implement", parent="review-1",
          created_at=RECEIVED_AT + 30)
    _task(queue, "review-2", "review", parent="fix-1", verdict="approve",
          created_at=RECEIVED_AT + 40)
    _task(queue, "test-405", "test", parent="review-2", status=test_status,
          created_at=RECEIVED_AT + 50)


def _set_context(queue, task_id, **changes):
    with sqlite3.connect(queue.db_path) as conn:
        row = conn.execute("SELECT context FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        context = json.loads(row[0])
        context.update(changes)
        conn.execute("UPDATE tasks SET context=? WHERE task_id=?",
                     (json.dumps(context), task_id))


def test_late_bind_adopts_completed_corrected_lineage_and_reconciles(queue):
    _pipeline(queue)

    result = queue.bind_work_intent(OP_ID, "impl-405")

    assert result["state"] == "SUCCESS"
    assert result["task_ids"] == ["impl-405", "review-1", "fix-1",
                                  "review-2", "test-405"]
    assert all(queue.get_task(task_id).context["external_operation_id"] == OP_ID
               for task_id in result["task_ids"])
    assert queue.get_task("test-405").context["issue"] == 405
    assert queue.get_task("test-405").context["risk_tier"] == 2


def test_rebind_repairs_existing_single_task_intent(queue):
    _task(queue, "impl-405", "implement")
    first = queue.bind_work_intent(OP_ID, "impl-405")
    assert first["state"] == "BOUND"
    assert first["task_ids"] == ["impl-405"]
    _task(queue, "review-1", "review", parent="impl-405", verdict="approve")
    _task(queue, "test-405", "test", parent="review-1")

    repaired = queue.bind_work_intent(OP_ID, "impl-405")

    assert repaired["state"] == "SUCCESS"
    assert repaired["task_ids"] == ["impl-405", "review-1", "test-405"]


def test_active_test_keeps_intent_bound_until_normal_result(queue):
    _pipeline(queue, test_status="in_progress")

    bound = queue.bind_work_intent(OP_ID, "impl-405")
    assert bound["state"] == "BOUND"
    assert len(bound["task_ids"]) == 5

    queue.submit_result("test-405", TaskResult(task_id="test-405",
                                                status="completed", summary="passed"))

    assert queue.get_work_intent(OP_ID)["state"] == "SUCCESS"


def test_older_descendant_is_not_taken(queue):
    _task(queue, "impl-405", "implement")
    _task(queue, "review-old", "review", parent="impl-405",
          created_at=RECEIVED_AT - 1, verdict="approve")
    _task(queue, "test-old", "test", parent="review-old")

    bound = queue.bind_work_intent(OP_ID, "impl-405")

    assert bound["state"] == "BOUND"
    assert bound["task_ids"] == ["impl-405"]


def test_foreign_bound_descendant_stops_its_branch(queue):
    _task(queue, "impl-405", "implement")
    _task(queue, "review-foreign", "review", parent="impl-405",
          verdict="approve")
    with sqlite3.connect(queue.db_path) as conn:
        row = conn.execute("SELECT context FROM tasks WHERE task_id='review-foreign'").fetchone()
        context = json.loads(row[0])
        context["external_operation_id"] = "another-operation"
        conn.execute("UPDATE tasks SET context=? WHERE task_id='review-foreign'",
                     (json.dumps(context),))
    _task(queue, "test-foreign", "test", parent="review-foreign")

    bound = queue.bind_work_intent(OP_ID, "impl-405")

    assert bound["task_ids"] == ["impl-405"]
    assert queue.get_task("review-foreign").context["external_operation_id"] == (
        "another-operation")


@pytest.mark.parametrize("status", ["needs_human", "blocked", "expired", "canceled"])
def test_blocking_descendant_and_its_subtree_are_bound(queue, status):
    _task(queue, "impl-405", "implement")
    _task(queue, "review-h", "review", parent="impl-405", status=status)
    _task(queue, "test-h", "test", parent="review-h")
    _task(queue, "review-1", "review", parent="impl-405", verdict="approve")
    _task(queue, "test-405", "test", parent="review-1")

    result = queue.bind_work_intent(OP_ID, "impl-405")

    assert result["state"] == "BLOCKED"
    assert set(result["task_ids"]) == {
        "impl-405", "review-h", "test-h", "review-1", "test-405"}
    assert result["blocker"]["blocker_type"] == "bound_tasks_unsuccessful"
    assert f"review-h={status}" in result["blocker"]["reason"]


@pytest.mark.parametrize("changes", [
    {"issue": 406}, {"repo": "owner/other"},
    {"risk_tier": 4}, {"risk_tier": True}, {"risk_tier": "2"},
])
def test_invalid_descendant_stops_its_subtree(queue, changes):
    _task(queue, "impl-405", "implement")
    _task(queue, "review-invalid", "review", parent="impl-405", verdict="approve")
    _set_context(queue, "review-invalid", **changes)
    _task(queue, "test-child", "test", parent="review-invalid")

    result = queue.bind_work_intent(OP_ID, "impl-405")

    assert result["state"] == "BOUND"
    assert result["task_ids"] == ["impl-405"]
    assert "external_operation_id" not in queue.get_task("review-invalid").context
    assert "external_operation_id" not in queue.get_task("test-child").context


def test_failed_fix_is_superseded_by_completed_system_retry(queue):
    _task(queue, "impl-405", "implement")
    _task(queue, "review-1", "review", parent="impl-405",
          verdict="request_changes")
    _task(queue, "fix-failed", "implement", parent="review-1", status="failed")
    _task(queue, "fix-retry", "implement", parent="fix-failed",
          retry_of="fix-failed")
    _task(queue, "review-2", "review", parent="fix-retry", verdict="approve")
    _task(queue, "test-405", "test", parent="review-2")

    result = queue.bind_work_intent(OP_ID, "impl-405")

    assert result["state"] == "SUCCESS"
    assert len(result["task_ids"]) == 6
    assert "fix-failed" in result["decision_reason"]


def test_no_descendants_preserves_completed_implement_behavior(queue):
    _task(queue, "impl-405", "implement")

    result = queue.bind_work_intent(OP_ID, "impl-405")

    assert result["state"] == "BOUND"
    assert result["task_ids"] == ["impl-405"]
