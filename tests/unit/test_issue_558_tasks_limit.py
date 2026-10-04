"""GET /tasks limits rows in SQL while preserving the existing display order."""

import dataclasses
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


@pytest.fixture
def populated_queue(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    rows = (
        ("first", 3, "pending"),
        ("second", 1, "failed"),
        ("third", 2, "pending"),
        ("fourth", 1, "pending"),
        ("fifth", 4, "failed"),
    )
    with sqlite3.connect(queue._db_path) as conn:
        conn.executemany(
            "INSERT INTO tasks "
            "(task_id, task_type, description, priority, context, status, created_at, receipt_id) "
            "VALUES (?, 'implement', 'work', ?, '{}', ?, ?, ?)",
            # Deliberately reverse timestamps so "newest" must mean rowid,
            # while the final display order still uses priority/created_at.
            [(task_id, priority, status, float(len(rows) - index), f"receipt-{task_id}")
             for index, (task_id, priority, status) in enumerate(rows)],
        )
    return queue


def _client(queue):
    app = create_app(
        queue._db_path, project="demo", pane_map={}, worktree_map={},
        watchdog_disabled=True, anomaly_disabled=True,
    )
    return TestClient(app, headers={"X-Agent-Crew-Project": "demo"})


def test_limit_returns_newest_rows_in_existing_display_order(populated_queue):
    queue = populated_queue
    assert [task.task_id for task in queue.list_tasks()] == [
        "fourth", "second", "third", "first", "fifth",
    ]
    assert [task.task_id for task in queue.list_tasks(limit=3)] == [
        "fourth", "third", "fifth",
    ]
    with _client(queue) as client:
        response = client.get("/tasks", params={"limit": 3})
    assert response.status_code == 200
    assert [task["task_id"] for task in response.json()] == [
        "fourth", "third", "fifth",
    ]


def test_status_filter_applies_before_limit(populated_queue):
    queue = populated_queue
    assert [task.task_id for task in queue.list_tasks(status="pending", limit=2)] == [
        "fourth", "third",
    ]
    with _client(queue) as client:
        response = client.get("/tasks", params={"status": "pending", "limit": 2})
    assert response.status_code == 200
    assert [task["task_id"] for task in response.json()] == ["fourth", "third"]


def test_no_limit_keeps_the_full_response(populated_queue):
    queue = populated_queue
    expected = [dataclasses.asdict(task) for task in queue.list_tasks()]
    with _client(queue) as client:
        response = client.get("/tasks")
    assert response.status_code == 200
    assert response.json() == expected


@pytest.mark.parametrize("limit", ["0", "not-an-integer"])
def test_invalid_limit_is_rejected(populated_queue, limit):
    with _client(populated_queue) as client:
        response = client.get("/tasks", params={"limit": limit})
    assert response.status_code == 422


def test_limit_does_not_decode_older_rows(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    with sqlite3.connect(queue._db_path) as conn:
        conn.executemany(
            "INSERT INTO tasks "
            "(task_id, task_type, description, context, created_at, receipt_id) "
            "VALUES (?, 'implement', 'work', ?, ?, ?)",
            [("old", "not JSON", 1.0, "receipt-old"),
             ("new", "{}", 2.0, "receipt-new")],
        )
    assert [task.task_id for task in queue.list_tasks(limit=1)] == ["new"]
