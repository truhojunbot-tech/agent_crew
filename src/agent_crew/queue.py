import json
import sqlite3
import time
from typing import List, Optional

from agent_crew.protocol import TaskRequest, TaskResult

_ROLE_TO_TYPE = {
    "coder": "implement",
    "reviewer": "review",
    "tester": "test",
    "panel": "discuss",
}

_DDL = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    task_type   TEXT NOT NULL,
    description TEXT NOT NULL,
    branch      TEXT NOT NULL DEFAULT '',
    priority    INTEGER NOT NULL DEFAULT 3,
    context     TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  REAL NOT NULL,
    summary     TEXT,
    verdict     TEXT,
    findings    TEXT,
    pr_number   INTEGER
)
"""


class TaskQueue:
    def __init__(self, db_path: str):
        self._db_path = db_path
        conn = self._connect()
        conn.execute(_DDL)
        conn.commit()
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def enqueue(self, task: TaskRequest) -> str:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO tasks (task_id, task_type, description, branch, priority, context, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    task.task_id,
                    task.task_type,
                    task.description,
                    task.branch,
                    task.priority,
                    json.dumps(task.context),
                    time.time(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return task.task_id

    def dequeue(self, agent: str = "", role: str = "") -> Optional[TaskRequest]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            task_type_filter = _ROLE_TO_TYPE.get(role) if role else None
            if task_type_filter:
                row = conn.execute(
                    """
                    SELECT * FROM tasks
                    WHERE status = 'pending' AND task_type = ?
                    ORDER BY priority ASC, created_at ASC
                    LIMIT 1
                    """,
                    (task_type_filter,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM tasks
                    WHERE status = 'pending'
                    ORDER BY priority ASC, created_at ASC
                    LIMIT 1
                    """
                ).fetchone()

            if row is None:
                conn.execute("ROLLBACK")
                return None

            conn.execute(
                "UPDATE tasks SET status = 'in_progress' WHERE task_id = ?",
                (row["task_id"],),
            )
            conn.execute("COMMIT")

            return TaskRequest(
                task_id=row["task_id"],
                task_type=row["task_type"],
                description=row["description"],
                branch=row["branch"],
                priority=row["priority"],
                context=json.loads(row["context"]),
            )
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def submit_result(self, task_id: str, result: TaskResult) -> None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT task_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise ValueError(f"Task not found: {task_id!r}")
            conn.execute(
                """
                UPDATE tasks
                SET status = 'completed', summary = ?, verdict = ?, findings = ?, pr_number = ?
                WHERE task_id = ?
                """,
                (
                    result.summary,
                    result.verdict,
                    json.dumps(result.findings),
                    result.pr_number,
                    task_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def cancel(self, task_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute("UPDATE tasks SET status = 'cancelled' WHERE task_id = ?", (task_id,))
            conn.commit()
        finally:
            conn.close()

    def list_tasks(self, status: str = "") -> List[TaskRequest]:
        conn = self._connect()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY priority ASC, created_at ASC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks ORDER BY priority ASC, created_at ASC"
                ).fetchall()
            return [
                TaskRequest(
                    task_id=r["task_id"],
                    task_type=r["task_type"],
                    description=r["description"],
                    branch=r["branch"],
                    priority=r["priority"],
                    context=json.loads(r["context"]),
                )
                for r in rows
            ]
        finally:
            conn.close()
