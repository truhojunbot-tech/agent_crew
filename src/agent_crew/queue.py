import json
import sqlite3
import time
from typing import List, Optional

from agent_crew.protocol import GateRequest, TaskRequest, TaskResult

_ROLE_TO_TYPE = {
    "coder": "implement",
    "implementer": "implement",
    "reviewer": "review",
    "tester": "test",
    "panel": "discuss",
}

# Reverse map (canonical role name per task_type). Used by the server to
# pick which pane to push a new task to.
_TYPE_TO_ROLE = {
    "implement": "implementer",
    "review": "reviewer",
    "test": "tester",
    "discuss": "panel",
}

_DDL_GATES = """
CREATE TABLE IF NOT EXISTS gates (
    id         TEXT PRIMARY KEY,
    type       TEXT NOT NULL,
    message    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL
)
"""

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

_DDL_CHECKPOINTS = """
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    checkpoint_num INTEGER NOT NULL,
    timestamp     REAL NOT NULL,
    state_snapshot TEXT NOT NULL,
    created_at    REAL NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
    UNIQUE(task_id, checkpoint_num)
)
"""


class TaskQueue:
    def __init__(self, db_path: str):
        self._db_path = db_path
        conn = self._connect()
        conn.execute(_DDL)
        conn.execute(_DDL_GATES)
        conn.execute(_DDL_CHECKPOINTS)
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
            if role:
                task_type_filter = _ROLE_TO_TYPE.get(role)
                if task_type_filter is None:
                    conn.execute("ROLLBACK")
                    raise ValueError(f"Unknown role: {role!r}. Must be one of {list(_ROLE_TO_TYPE)}")
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

    def submit_result(self, task_id: str, result: TaskResult) -> str:
        """Submit a task result. Returns the task_type of the completed task
        (so push-model callers can decide what to push next)."""
        conn = self._connect()
        try:
            if result.task_id != task_id:
                raise ValueError(f"task_id mismatch: argument {task_id!r} != result.task_id {result.task_id!r}")
            row = conn.execute("SELECT task_type FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise ValueError(f"Task not found: {task_id!r}")
            task_type = row["task_type"]
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, summary = ?, verdict = ?, findings = ?, pr_number = ?
                WHERE task_id = ?
                """,
                (
                    result.status,
                    result.summary,
                    result.verdict,
                    json.dumps(result.findings),
                    result.pr_number,
                    task_id,
                ),
            )
            conn.commit()
            return task_type
        finally:
            conn.close()

    def cancel(self, task_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute("UPDATE tasks SET status = 'cancelled' WHERE task_id = ?", (task_id,))
            conn.commit()
        finally:
            conn.close()

    def has_in_progress(self, task_type: str) -> bool:
        """Return True if any task of the given type is in_progress.
        Used by push-model server to decide if a role is busy."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM tasks WHERE status = 'in_progress' AND task_type = ? LIMIT 1",
                (task_type,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def has_discuss_in_progress_for_agent(self, agent: str) -> bool:
        """Per-agent busy check for discuss tasks. Needed because discuss tasks
        fan out to different panes (one per agent) and the coarse `has_in_progress`
        would falsely mark a pane busy when a sibling panelist is mid-reply."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT context FROM tasks WHERE status = 'in_progress' AND task_type = 'discuss'"
            ).fetchall()
            for r in rows:
                try:
                    ctx = json.loads(r["context"]) if r["context"] else {}
                except Exception:
                    continue
                if ctx.get("agent") == agent:
                    return True
            return False
        finally:
            conn.close()

    def dequeue_discuss_for_agent(self, agent: str) -> Optional[TaskRequest]:
        """Atomic pending→in_progress for the oldest pending discuss task whose
        context.agent matches `agent`. Context is stored as JSON, so filtering
        happens in Python under BEGIN IMMEDIATE to keep the read+update atomic."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'pending' AND task_type = 'discuss'
                ORDER BY priority ASC, created_at ASC
                """
            ).fetchall()
            chosen = None
            for row in rows:
                try:
                    ctx = json.loads(row["context"]) if row["context"] else {}
                except Exception:
                    continue
                if ctx.get("agent") == agent:
                    chosen = row
                    break
            if chosen is None:
                conn.execute("ROLLBACK")
                return None
            conn.execute(
                "UPDATE tasks SET status = 'in_progress' WHERE task_id = ?",
                (chosen["task_id"],),
            )
            conn.execute("COMMIT")
            return TaskRequest(
                task_id=chosen["task_id"],
                task_type=chosen["task_type"],
                description=chosen["description"],
                branch=chosen["branch"],
                priority=chosen["priority"],
                context=json.loads(chosen["context"]),
            )
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def get_task_context(self, task_id: str) -> dict:
        """Return the stored context dict for a task, or {} if not found."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT context FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None or not row["context"]:
                return {}
            try:
                return json.loads(row["context"])
            except Exception:
                return {}
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

    def create_gate(self, gate: GateRequest) -> str:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO gates (id, type, message, status, created_at) VALUES (?, ?, ?, ?, ?)",
                (gate.id, gate.type, gate.message, "pending", gate.created_at),
            )
            conn.commit()
        finally:
            conn.close()
        return gate.id

    def resolve_gate(self, gate_id: str, approved: bool) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM gates WHERE id = ?", (gate_id,)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise ValueError(f"Gate not found: {gate_id!r}")
            if row["status"] in ("approved", "rejected"):
                conn.execute("ROLLBACK")
                raise ValueError(f"Gate {gate_id!r} is already resolved (status={row['status']!r})")
            new_status = "approved" if approved else "rejected"
            conn.execute("UPDATE gates SET status = ? WHERE id = ?", (new_status, gate_id))
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def list_gates(self, status: str = "") -> List[GateRequest]:
        conn = self._connect()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM gates WHERE status = ? ORDER BY created_at ASC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM gates ORDER BY created_at ASC").fetchall()
            return [
                GateRequest(
                    id=r["id"],
                    type=r["type"],
                    message=r["message"],
                    status=r["status"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]
        finally:
            conn.close()

    def get_result(self, task_id: str) -> Optional[TaskResult]:
        """Return TaskResult if the task is done, else None."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT task_id, status, summary, verdict, findings FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None or row["status"] not in ("completed", "failed", "needs_human"):
                return None
            return TaskResult(
                task_id=row["task_id"],
                status=row["status"],
                summary=row["summary"] or "",
                verdict=row["verdict"],
                findings=json.loads(row["findings"]) if row["findings"] else [],
            )
        finally:
            conn.close()

    def save_checkpoint(self, task_id: str, checkpoint_num: int, state_snapshot: dict) -> str:
        """Save a checkpoint for a task. Returns checkpoint_id."""
        checkpoint_id = f"ckpt-{task_id}-{checkpoint_num}"
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO checkpoints
                (checkpoint_id, task_id, checkpoint_num, timestamp, state_snapshot, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_id,
                    task_id,
                    checkpoint_num,
                    time.time(),
                    json.dumps(state_snapshot),
                    time.time(),
                ),
            )
            conn.commit()
            return checkpoint_id
        finally:
            conn.close()

    def get_checkpoint(self, task_id: str, checkpoint_num: int) -> Optional[dict]:
        """Retrieve a specific checkpoint for a task."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT state_snapshot FROM checkpoints WHERE task_id = ? AND checkpoint_num = ?",
                (task_id, checkpoint_num),
            ).fetchone()
            if row is None:
                return None
            return json.loads(row["state_snapshot"])
        finally:
            conn.close()

    def get_latest_checkpoint(self, task_id: str) -> Optional[tuple]:
        """Retrieve the latest checkpoint for a task. Returns (checkpoint_num, state_snapshot)."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT checkpoint_num, state_snapshot FROM checkpoints WHERE task_id = ? ORDER BY checkpoint_num DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            return (row["checkpoint_num"], json.loads(row["state_snapshot"]))
        finally:
            conn.close()

    def list_checkpoints(self, task_id: str) -> List[dict]:
        """List all checkpoints for a task."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT checkpoint_num, timestamp FROM checkpoints WHERE task_id = ? ORDER BY checkpoint_num ASC",
                (task_id,),
            ).fetchall()
            return [
                {
                    "checkpoint_num": r["checkpoint_num"],
                    "timestamp": r["timestamp"],
                }
                for r in rows
            ]
        finally:
            conn.close()
