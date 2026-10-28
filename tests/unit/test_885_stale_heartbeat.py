"""#885 — stale-task expiry must count heartbeats.

Dispatcher workers report liveness through ``record_heartbeat``, which writes
``last_heartbeat_at`` only. ``expire_stale`` and ``reset_stale_to_pending``
used to read ``last_activity_at`` alone, so a live worker with a fresh
heartbeat could be cancelled or reset. A reset also left the lease and claim
fields set and wrote no ``requeued`` event.
"""
from __future__ import annotations

import sqlite3
import time

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


def _in_progress(tmp_path, task_id, *, activity_ago=1200.0, heartbeat_ago=None):
    db = str(tmp_path / "tasks.db")
    q = TaskQueue(db)
    q.enqueue(TaskRequest(task_id=task_id, task_type="implement",
                          description="t", branch="main"))
    now = time.time()
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE tasks SET status='in_progress', claim_source='dispatcher', "
        "lease_owner='dispatcher:w1', lease_expires_at=?, last_activity_at=? "
        "WHERE task_id=?",
        (now + 600, now - activity_ago, task_id),
    )
    conn.commit()
    conn.close()
    if heartbeat_ago is not None:
        q.record_heartbeat(task_id, source="output_progress", ts=now - heartbeat_ago)
    return db, q


def _row(db, task_id):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM tasks WHERE task_id=?",
                                 (task_id,)).fetchone())
    finally:
        conn.close()


def test_expire_stale_spares_task_with_fresh_heartbeat(tmp_path):
    db, q = _in_progress(tmp_path, "live", heartbeat_ago=30)
    assert q.expire_stale(older_than_seconds=600, dry_run=True) == []
    assert q.expire_stale(older_than_seconds=600) == []
    assert _row(db, "live")["status"] == "in_progress"


def test_expire_stale_still_cancels_when_heartbeat_also_stale(tmp_path):
    db, q = _in_progress(tmp_path, "dead", heartbeat_ago=1200)
    assert q.expire_stale(older_than_seconds=600) == ["dead"]
    assert _row(db, "dead")["status"] == "cancelled"


def test_expire_stale_heartbeat_old_but_activity_fresh_is_spared(tmp_path):
    db, q = _in_progress(tmp_path, "act", activity_ago=30, heartbeat_ago=1200)
    assert q.expire_stale(older_than_seconds=600) == []


def test_reset_stale_spares_task_with_fresh_heartbeat(tmp_path):
    db, q = _in_progress(tmp_path, "live", heartbeat_ago=30)
    assert q.reset_stale_to_pending(older_than_seconds=600) == []
    row = _row(db, "live")
    assert row["status"] == "in_progress"
    assert row["lease_owner"] == "dispatcher:w1"


def test_reset_stale_clears_lease_and_claim_and_logs_requeued(tmp_path):
    db, q = _in_progress(tmp_path, "dead", heartbeat_ago=1200)
    assert q.reset_stale_to_pending(older_than_seconds=600) == ["dead"]
    row = _row(db, "dead")
    assert row["status"] == "pending"
    assert row["claim_source"] == ""
    assert row["lease_owner"] is None
    assert row["lease_expires_at"] is None
    events = [e["event"] for e in q.get_exec_state("dead")["events"]]
    assert "requeued" in events
