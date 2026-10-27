"""#342: expose root recall applicability in task attribution for quota-core."""

import json
import sqlite3

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


def test_recall_applicability_migrates_existing_attribution_idempotently(tmp_path):
    db = str(tmp_path / "tasks.db")
    TaskQueue(db)
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE task_attribution DROP COLUMN recall_not_applicable")

    TaskQueue(db)
    TaskQueue(db)
    with sqlite3.connect(db) as conn:
        columns = conn.execute("PRAGMA table_info(task_attribution)").fetchall()
    assert sum(column[1] == "recall_not_applicable" for column in columns) == 1


def test_root_recall_is_nullable_attribution_and_exported_economics(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("root", "implement", "root"))
    queue.record_attribution("root")
    queue.record_required_context_recalled("root", None, recall_not_applicable=True)
    attribution = queue.get_attribution("root")
    assert attribution["recall_not_applicable"] == 1
    assert attribution["required_context_recalled"] is None

    queue.submit_result("root", TaskResult("root", "completed", "done"))
    receipt = queue.get_tokenomics_shadow_receipt("root")
    economics = json.loads(receipt["economics_json"])
    assert economics["recall_not_applicable"] == 1
    assert economics["required_context_recalled"] is None
    assert json.loads(receipt["evidence_json"])["recall_not_applicable"] is True
    exported = queue.export_project_runtime_state()["worker_receipts"]
    assert next(row for row in exported if row["task_id"] == "root")[
        "recall_not_applicable"] == 1


def test_predecessor_recall_keeps_applicability_unknown(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("fix", "implement", "fix",
                              context={"prev_task_id": "prior"}))
    queue.record_attribution("fix")
    queue.record_required_context_recalled("fix", False)
    attribution = queue.get_attribution("fix")
    assert attribution["recall_not_applicable"] is None
    assert attribution["required_context_recalled"] == 0
    queue.submit_result("fix", TaskResult("fix", "completed", "done"))
    economics = json.loads(queue.get_tokenomics_shadow_receipt("fix")["economics_json"])
    assert economics["recall_not_applicable"] is None
    assert economics["required_context_recalled"] == 0
