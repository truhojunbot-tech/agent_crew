"""#830: a periodic tick must not read and sort the whole tasks table.

``reconcile_all_cloud_tasks`` (watchdog every 30s, dispatcher every <=10s with
cloud on) and ``reconcile_shadow_reviews`` iterated ``queue.list_tasks()``:
``SELECT * FROM tasks ORDER BY priority ASC, created_at ASC``. No index serves
that order, so SQLite sorted every row through an unlinked temp file on each
call — the write_bytes that /proc reported as cancelled — and held the read
lock while it did, which is what POST /tasks lost to as ``database is locked``.
"""

import re
import time

import pytest
from fastapi.testclient import TestClient

from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app

_FULL_TABLE = re.compile(r"\bFROM\s+tasks\s+ORDER\s+BY\b", re.IGNORECASE)


@pytest.fixture
def statements(monkeypatch):
    """Every SQL statement any TaskQueue connection runs."""
    seen = []
    original = TaskQueue._connect

    def _connect(self):
        conn = original(self)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(TaskQueue, "_connect", _connect)
    return seen


def _full_table_reads(statements):
    return [s for s in statements if _FULL_TABLE.search(" ".join(s.split()))]


def _enqueue(queue, task_id, context=None, task_type="implement"):
    queue.enqueue(TaskRequest(task_id=task_id, task_type=task_type, description="d",
                              branch=f"agent/{task_id}", project="agent_crew",
                              context=context or {}))


def test_cloud_reconcile_does_not_read_the_whole_table(tmp_path, monkeypatch, statements):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    for i in range(5):
        _enqueue(queue, f"plain-{i}")
    recorded = []
    monkeypatch.setattr(cloud, "_record_reconciled_cloud_cost",
                        lambda q, task: recorded.append(task.task_id))
    statements.clear()

    cloud.reconcile_all_cloud_tasks(queue)

    assert _full_table_reads(statements) == []
    assert recorded == []


def test_shadow_reconcile_does_not_read_the_whole_table(tmp_path, statements):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    for i in range(5):
        _enqueue(queue, f"plain-{i}", task_type="review")
    statements.clear()

    cloud.reconcile_shadow_reviews(queue, pr_comments_fn=lambda *a, **k: [])

    assert _full_table_reads(statements) == []


def test_cloud_reconcile_still_settles_pending_cost(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _enqueue(queue, "pending-cost")
    queue.patch_context("pending-cost", {"cloud_cost_pending_at": 1000.0})
    queue.cancel("pending-cost")
    _enqueue(queue, "plain")
    queue.cancel("plain")
    recorded = []
    monkeypatch.setattr(cloud, "_record_reconciled_cloud_cost",
                        lambda q, task: recorded.append(task.task_id))

    cloud.reconcile_all_cloud_tasks(queue)

    assert recorded == ["pending-cost"]


def test_shadow_reconcile_still_settles_pending_cost(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _enqueue(queue, "shadow", task_type="review")
    queue.patch_context("shadow", {"cloud_shadow_cost_pending_at": 1000.0})
    _enqueue(queue, "cloud-only", task_type="review")
    queue.patch_context("cloud-only", {"cloud_cost_pending_at": 1000.0})
    recorded = []
    monkeypatch.setattr(cloud, "_record_shadow_review_cost",
                        lambda q, task_id: recorded.append(task_id))

    cloud.reconcile_shadow_reviews(queue, pr_comments_fn=lambda *a, **k: [])

    assert recorded == ["shadow"]


def test_context_key_prefix_is_literal_and_keeps_list_order(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _enqueue(queue, "underscore-wildcard", {"cloudXusd": 1})   # `_` is not a wildcard
    _enqueue(queue, "no-key", {"note": "cloud"})               # unquoted prefix
    _enqueue(queue, "low", {"cloud_usd": 1})
    queue.enqueue(TaskRequest(task_id="high", task_type="implement", description="d",
                              branch="agent/high", project="agent_crew", priority=1,
                              context={"cloud_cost_pending_at": 1}))

    found = [t.task_id for t in queue.list_tasks_with_context_key_prefix("cloud_")]

    assert found == ["high", "low"]
    assert found == [t.task_id for t in queue.list_tasks() if t.task_id in found]


def test_idle_watchdog_tick_does_not_read_the_whole_table(tmp_path, monkeypatch, statements):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    db = str(tmp_path / "tasks.db")
    app = create_app(db, project="agent_crew", pane_map={}, identity_required=False,
                     watchdog_disabled=True, anomaly_disabled=True,
                     coordinator_notify_fn=lambda message: True)
    with TestClient(app):
        queue = TaskQueue(db)
        for i in range(5):
            _enqueue(queue, f"done-{i}")
            queue.cancel(f"done-{i}")
        statements.clear()

        app.state.watchdog_tick(time.time())

    assert statements, "the trace saw no queue SQL — the harness is not observing the tick"
    assert _full_table_reads(statements) == []
