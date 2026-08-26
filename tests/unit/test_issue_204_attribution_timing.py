"""Issue #204: reliable task_attribution timing windows.

record_attribution() was already called at actual dispatch time (not queue-
creation time), and its started_at parameter already defaulted to "now" via
`started_at or now` — so the acceptance criterion "populate started_at at
the dispatch boundary" was, on inspection, already satisfied for a task's
*first* dispatch.

The real gap: task_attribution is upserted by task_id, and a transient-
error retry (#199/#205) or a restart/recovery re-dispatch calls
record_attribution() again for that *same* task_id. Because started_at was
included in the ON CONFLICT DO UPDATE clause, every re-dispatch silently
overwrote it with the retry's own timestamp — erasing the original
first-attempt start time and making it impossible to tell real execution
time apart from queue/retry wait time.
"""
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


def _enqueue(queue: TaskQueue, task_id: str) -> None:
    queue.enqueue(TaskRequest(
        task_id=task_id,
        task_type="implement",
        description="test task",
        branch="main",
    ))


def test_u204_started_at_set_on_first_dispatch(tmp_db):
    queue = TaskQueue(tmp_db)
    _enqueue(queue, "task-204a")

    queue.record_attribution(task_id="task-204a", started_at=1000.0)

    row = queue.get_attribution("task-204a")
    assert row["started_at"] == 1000.0


def test_u204_started_at_preserved_across_same_task_id_redispatch(tmp_db):
    """Simulates a transient-error retry: the dispatcher requeues the same
    task_id and record_attribution() runs again for it on the re-dispatch."""
    queue = TaskQueue(tmp_db)
    _enqueue(queue, "task-204b")

    queue.record_attribution(task_id="task-204b", started_at=1000.0)
    # Re-dispatch (retry) — a real caller wouldn't pass started_at at all
    # (server.py never does; it relies on `started_at or now`), so this
    # exercises the exact call shape production makes on a retry.
    queue.record_attribution(task_id="task-204b", status="in_progress")

    row = queue.get_attribution("task-204b")
    assert row["started_at"] == 1000.0, "retry re-dispatch must not overwrite the original start time"


def test_u204_started_at_preserved_across_multiple_redispatches(tmp_db):
    """The agy_subscriber_lag pattern observed in production: the same
    task_id gets re-dispatched several times in a row before succeeding."""
    queue = TaskQueue(tmp_db)
    _enqueue(queue, "task-204c")

    queue.record_attribution(task_id="task-204c", started_at=1000.0)
    for _ in range(4):
        queue.record_attribution(task_id="task-204c", status="in_progress")

    row = queue.get_attribution("task-204c")
    assert row["started_at"] == 1000.0


def test_u204_other_fields_still_update_on_redispatch(tmp_db):
    """The started_at fix must not turn the whole upsert into a no-op —
    everything else (status, context lineage, etc.) still refreshes."""
    queue = TaskQueue(tmp_db)
    _enqueue(queue, "task-204d")

    queue.record_attribution(task_id="task-204d", started_at=1000.0, context_generation=1)
    queue.record_attribution(task_id="task-204d", status="in_progress", context_generation=2)

    row = queue.get_attribution("task-204d")
    assert row["started_at"] == 1000.0
    assert row["context_generation"] == 2


def test_u204_completed_at_set_on_terminal_success(tmp_db):
    queue = TaskQueue(tmp_db)
    _enqueue(queue, "task-204e")
    queue.record_attribution(task_id="task-204e", started_at=1000.0)

    queue.submit_result("task-204e", TaskResult(
        task_id="task-204e", status="completed", summary="done",
    ))

    row = queue.get_attribution("task-204e")
    assert row["started_at"] == 1000.0
    assert row["completed_at"] >= row["started_at"]
    assert row["status"] == "completed"
    assert row["outcome"] == "completed"


def test_u204_timing_invariant_violation_is_logged(tmp_db, caplog):
    """completed_at < started_at should never happen in practice (started_at
    is anchored to the past by construction), but if a corrupted row or
    clock skew produces it, that must be surfaced rather than silently
    accepted (#204 acceptance criteria)."""
    import logging

    queue = TaskQueue(tmp_db)
    _enqueue(queue, "task-204f")
    # A started_at far in the future relative to "now" simulates the
    # invariant being violated without needing to fake time.time() itself.
    far_future = 4102444800.0  # 2100-01-01
    queue.record_attribution(task_id="task-204f", started_at=far_future)

    with caplog.at_level(logging.WARNING, logger="agent_crew.queue"):
        queue.submit_result("task-204f", TaskResult(
            task_id="task-204f", status="completed", summary="done",
        ))

    assert any("timing invariant violated" in r.message for r in caplog.records)
