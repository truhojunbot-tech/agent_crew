"""Legacy task migration isolates rows that cannot pass current validators."""

import json

import pytest
from fastapi.testclient import TestClient

from agent_crew.pipeline import auto_enqueue_review
from agent_crew.protocol import TaskRequest
from agent_crew.queue import MalformedStoredRow, TaskQueue
from agent_crew.server import create_app


def _task(task_id):
    return TaskRequest(task_id, "implement", "fixture", branch="feature",
                       context={"risk_tier": 1, "crew_run_branch": True})


def _legacy_row(queue, task_id, context):
    conn = queue._connect()
    try:
        conn.execute("UPDATE tasks SET context=? WHERE task_id=?",
                     (json.dumps(context), task_id))
        conn.execute("DELETE FROM task_metadata_migrations WHERE name='legacy_task_rows_v1'")
        conn.commit()
    finally:
        conn.close()


def test_numeric_string_fix_round_is_migrated_once(tmp_path):
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    queue.enqueue(_task("old-round"))
    _legacy_row(queue, "old-round", {"risk_tier": 1, "fix_round": "05"})
    reopened = TaskQueue(db)
    assert reopened.get_task_context("old-round")["fix_round"] == 5
    assert [task.task_id for task in reopened.list_tasks()] == ["old-round"]
    assert TaskQueue(db).get_task_context("old-round")["fix_round"] == 5


def test_unconvertible_row_is_quarantined_and_noticed_once(tmp_path):
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    queue.enqueue(_task("bad-round"))
    queue.enqueue(_task("good-round"))
    _legacy_row(queue, "bad-round", {"risk_tier": 1, "fix_round": "unknown"})
    delivered = []
    app = create_app(db, project="demo", pane_map={}, worktree_map={},
                     watchdog_disabled=True, anomaly_disabled=True,
                     coordinator_notify_fn=lambda message: delivered.append(message) or True)
    with TestClient(app):
        pass
    with TestClient(app):
        pass
    reopened = TaskQueue(db)
    assert reopened.quarantine_field("bad-round") == "fix_round"
    assert [task.task_id for task in reopened.list_tasks()] == ["good-round"]
    review_id = auto_enqueue_review(reopened, "good-round", pr_number=972,
                                    pr_state_fn=lambda *_a, **_k: "open")
    assert review_id == "review-good-round-r0"
    with pytest.raises(MalformedStoredRow, match="quarantined fix_round"):
        reopened.get_task("bad-round")
    assert len(delivered) == 1
    assert "task=bad-round" in delivered[0] and "field=fix_round" in delivered[0]


def test_review_reads_known_task_without_scanning(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(_task("impl-972"))
    monkeypatch.setattr(queue, "list_tasks", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("review scanned all tasks")))
    review_id = auto_enqueue_review(queue, "impl-972", pr_number=972,
                                    pr_state_fn=lambda *_a, **_k: "open")
    assert review_id == "review-impl-972-r0"
    assert queue.get_task(review_id) is not None


def test_new_write_still_rejects_string_round():
    with pytest.raises(ValueError, match="fix_round"):
        TaskRequest("new-bad-972", "implement", "fixture",
                    context={"risk_tier": 1, "fix_round": "5"})
