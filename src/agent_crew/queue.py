import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from typing import List, Optional

from agent_crew.context_identity import CONTEXT_SCHEMA_VERSION
from agent_crew.protocol import GateRequest, TaskRequest, TaskResult

logger = logging.getLogger(__name__)

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
    task_id          TEXT PRIMARY KEY,
    task_type        TEXT NOT NULL,
    description      TEXT NOT NULL,
    branch           TEXT NOT NULL DEFAULT '',
    priority         INTEGER NOT NULL DEFAULT 3,
    context          TEXT NOT NULL DEFAULT '{}',
    status           TEXT NOT NULL DEFAULT 'pending',
    created_at       REAL NOT NULL,
    project          TEXT NOT NULL DEFAULT '',
    summary          TEXT,
    verdict          TEXT,
    findings         TEXT,
    pr_number        INTEGER,
    last_activity_at REAL NOT NULL DEFAULT 0
)
"""

_DDL_MIGRATE_PROJECT = "ALTER TABLE tasks ADD COLUMN project TEXT NOT NULL DEFAULT ''"
_DDL_MIGRATE_LAST_ACTIVITY = (
    "ALTER TABLE tasks ADD COLUMN last_activity_at REAL NOT NULL DEFAULT 0"
)
_DDL_MIGRATE_PUSH_AT = "ALTER TABLE tasks ADD COLUMN push_at REAL NOT NULL DEFAULT 0"
_DDL_MIGRATE_ERROR_INFO = "ALTER TABLE tasks ADD COLUMN error_info TEXT DEFAULT NULL"

_DDL_ATTRIBUTION = """
CREATE TABLE IF NOT EXISTS task_attribution (
    task_id          TEXT PRIMARY KEY,
    project          TEXT NOT NULL DEFAULT '',
    agent            TEXT NOT NULL DEFAULT '',
    role             TEXT NOT NULL DEFAULT '',
    task_type        TEXT NOT NULL DEFAULT '',
    worktree_path    TEXT NOT NULL DEFAULT '',
    codex_logs_path  TEXT NOT NULL DEFAULT '',
    repo_url         TEXT NOT NULL DEFAULT '',
    git_branch       TEXT NOT NULL DEFAULT '',
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
)
"""

# Durable context identity (#202) — one row per (project, agent,
# worktree_path). NOT keyed by role: agent_override can route a task from
# one role into another agent's worktree, and doing so genuinely resumes
# that agent's ongoing provider conversation regardless of which role
# nominally owns the task (Agent ≠ Role ≠ Context).
_DDL_CONTEXT_STATE = """
CREATE TABLE IF NOT EXISTS context_state (
    context_key          TEXT PRIMARY KEY,
    project              TEXT NOT NULL DEFAULT '',
    role                 TEXT NOT NULL DEFAULT '',
    agent                TEXT NOT NULL DEFAULT '',
    worktree_path        TEXT NOT NULL DEFAULT '',
    context_id           TEXT NOT NULL,
    context_generation   INTEGER NOT NULL DEFAULT 1,
    session_task_index   INTEGER NOT NULL DEFAULT 0,
    provider_session_id  TEXT,
    last_task_id         TEXT,
    created_at           REAL NOT NULL,
    updated_at           REAL NOT NULL
)
"""

# task_attribution migrations (#202) — durable context identity + lineage
# fields, added via the same defensive ALTER-TABLE pattern as the existing
# tasks-table migrations below. All nullable/defaulted so existing rows
# (written before this migration) remain valid.
_DDL_MIGRATE_ATTR_SCHEMA_VERSION = (
    "ALTER TABLE task_attribution ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1"
)
_DDL_MIGRATE_ATTR_MODEL = "ALTER TABLE task_attribution ADD COLUMN model TEXT DEFAULT ''"
_DDL_MIGRATE_ATTR_CONTEXT_ID = "ALTER TABLE task_attribution ADD COLUMN context_id TEXT DEFAULT ''"
_DDL_MIGRATE_ATTR_PROVIDER_SESSION_ID = (
    "ALTER TABLE task_attribution ADD COLUMN provider_session_id TEXT DEFAULT ''"
)
_DDL_MIGRATE_ATTR_CONTEXT_POLICY = (
    "ALTER TABLE task_attribution ADD COLUMN context_policy TEXT DEFAULT ''"
)
_DDL_MIGRATE_ATTR_CONTEXT_GENERATION = (
    "ALTER TABLE task_attribution ADD COLUMN context_generation INTEGER DEFAULT 0"
)
_DDL_MIGRATE_ATTR_SESSION_TASK_INDEX = (
    "ALTER TABLE task_attribution ADD COLUMN session_task_index INTEGER DEFAULT 0"
)
_DDL_MIGRATE_ATTR_PREVIOUS_TASK_ID = (
    "ALTER TABLE task_attribution ADD COLUMN previous_task_id TEXT DEFAULT ''"
)
_DDL_MIGRATE_ATTR_RETRY_OF = "ALTER TABLE task_attribution ADD COLUMN retry_of TEXT DEFAULT ''"
_DDL_MIGRATE_ATTR_FALLBACK_OF = "ALTER TABLE task_attribution ADD COLUMN fallback_of TEXT DEFAULT ''"
_DDL_MIGRATE_ATTR_STARTED_AT = "ALTER TABLE task_attribution ADD COLUMN started_at REAL DEFAULT 0"
_DDL_MIGRATE_ATTR_COMPLETED_AT = "ALTER TABLE task_attribution ADD COLUMN completed_at REAL DEFAULT 0"
_DDL_MIGRATE_ATTR_OUTCOME = "ALTER TABLE task_attribution ADD COLUMN outcome TEXT DEFAULT ''"

_DDL_MIGRATE_ATTRIBUTION_COLUMNS = (
    _DDL_MIGRATE_ATTR_SCHEMA_VERSION,
    _DDL_MIGRATE_ATTR_MODEL,
    _DDL_MIGRATE_ATTR_CONTEXT_ID,
    _DDL_MIGRATE_ATTR_PROVIDER_SESSION_ID,
    _DDL_MIGRATE_ATTR_CONTEXT_POLICY,
    _DDL_MIGRATE_ATTR_CONTEXT_GENERATION,
    _DDL_MIGRATE_ATTR_SESSION_TASK_INDEX,
    _DDL_MIGRATE_ATTR_PREVIOUS_TASK_ID,
    _DDL_MIGRATE_ATTR_RETRY_OF,
    _DDL_MIGRATE_ATTR_FALLBACK_OF,
    _DDL_MIGRATE_ATTR_STARTED_AT,
    _DDL_MIGRATE_ATTR_COMPLETED_AT,
    _DDL_MIGRATE_ATTR_OUTCOME,
)

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

# Performance indexes for common queries
_DDL_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_type_status ON tasks(task_type, status);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority DESC, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_checkpoints_task_num ON checkpoints(task_id, checkpoint_num DESC);
CREATE INDEX IF NOT EXISTS idx_gates_status ON gates(status);
"""


class TaskQueue:
    def __init__(self, db_path: str):
        self._db_path = db_path
        conn = self._connect()
        conn.execute(_DDL)
        conn.execute(_DDL_GATES)
        conn.execute(_DDL_ATTRIBUTION)
        conn.execute(_DDL_CHECKPOINTS)
        conn.execute(_DDL_CONTEXT_STATE)
        # Migrate existing DBs: add project column if absent
        try:
            conn.execute(_DDL_MIGRATE_PROJECT)
        except Exception:
            pass  # column already exists
        try:
            conn.execute(_DDL_MIGRATE_LAST_ACTIVITY)
        except Exception:
            pass  # column already exists
        try:
            conn.execute(_DDL_MIGRATE_PUSH_AT)
        except Exception:
            pass  # column already exists
        try:
            conn.execute(_DDL_MIGRATE_ERROR_INFO)
        except Exception:
            pass  # column already exists
        # #202: durable context identity + lineage columns on task_attribution.
        for _stmt in _DDL_MIGRATE_ATTRIBUTION_COLUMNS:
            try:
                conn.execute(_stmt)
            except Exception:
                pass  # column already exists
        # Create indexes for performance
        for idx_stmt in _DDL_INDEXES.strip().split('\n'):
            if idx_stmt.strip():
                conn.execute(idx_stmt)
        conn.commit()
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def enqueue(self, task: TaskRequest) -> str:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO tasks (task_id, task_type, description, branch, priority, context, status, created_at, project)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    task.task_id,
                    task.task_type,
                    task.description,
                    task.branch,
                    task.priority,
                    json.dumps(task.context),
                    time.time(),
                    task.project,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return task.task_id

    def patch_context(self, task_id: str, extra: dict) -> None:
        """Merge ``extra`` into the existing context of a pending task."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT context FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                return
            existing = json.loads(row["context"] or "{}")
            merged = {**existing, **extra}
            conn.execute(
                "UPDATE tasks SET context=? WHERE task_id=?",
                (json.dumps(merged), task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def dequeue(self, agent: str = "", role: str = "") -> Optional[TaskRequest]:
        """Atomically dequeue the next pending task for ``agent`` / ``role``.

        Resolution order (Issue #106 phase 3 — supports dynamic role
        reassignment via ``context.agent_override``):

        1. ``agent`` given: prefer tasks whose ``context.agent_override``
           equals ``agent``, regardless of task_type. Operator overrides
           (``crew run --reviewer gemini``) and the rate-limit fallback
           chain both flow through this path.
        2. ``role`` given (with or without ``agent``): tasks of the
           matching task_type WHERE either no override is set or the
           override claims this agent. The latter clause prevents an
           agent from stealing a task explicitly routed to another
           agent. Stage 2 only runs after stage 1 has no candidate.
        3. Neither given: any pending task, ordered by priority.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = None
            if agent:
                # Stage 1 — explicit override claim for this agent.
                row = conn.execute(
                    """
                    SELECT * FROM tasks
                    WHERE status = 'pending'
                      AND json_extract(context, '$.agent_override') = ?
                    ORDER BY priority ASC, created_at ASC
                    LIMIT 1
                    """,
                    (agent,),
                ).fetchone()

            if row is None and role:
                # Stage 2 — default role, skipping tasks claimed by others.
                task_type_filter = _ROLE_TO_TYPE.get(role)
                if task_type_filter is None:
                    conn.execute("ROLLBACK")
                    raise ValueError(f"Unknown role: {role!r}. Must be one of {list(_ROLE_TO_TYPE)}")
                if agent:
                    row = conn.execute(
                        """
                        SELECT * FROM tasks
                        WHERE status = 'pending' AND task_type = ?
                          AND (
                            json_extract(context, '$.agent_override') IS NULL
                            OR json_extract(context, '$.agent_override') = ?
                          )
                        ORDER BY priority ASC, created_at ASC
                        LIMIT 1
                        """,
                        (task_type_filter, agent),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """
                        SELECT * FROM tasks
                        WHERE status = 'pending' AND task_type = ?
                        ORDER BY priority ASC, created_at ASC
                        LIMIT 1
                        """,
                        (task_type_filter,),
                    ).fetchone()

            if row is None and not agent and not role:
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
                "UPDATE tasks SET status = 'in_progress', last_activity_at = ? WHERE task_id = ?",
                (time.time(), row["task_id"]),
            )
            conn.execute("COMMIT")

            return TaskRequest(
                task_id=row["task_id"],
                task_type=row["task_type"],
                description=row["description"],
                branch=row["branch"],
                priority=row["priority"],
                context=json.loads(row["context"]),
                project=row["project"] if row["project"] else "",
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
            # #167: persist structured error_info for failed results so post-mortem
            # debugging has machine-readable data, not just the free-form summary.
            error_info_json = None
            if result.status == "failed" and result.error_info:
                error_info_json = json.dumps(result.error_info)
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, summary = ?, verdict = ?, findings = ?, pr_number = ?, error_info = ?
                WHERE task_id = ?
                """,
                (
                    result.status,
                    result.summary,
                    result.verdict,
                    json.dumps(result.findings),
                    result.pr_number,
                    error_info_json,
                    task_id,
                ),
            )
            # #202: record the final outcome on the attribution row too, in
            # the same transaction, so it's set regardless of whether this
            # came from the agent's own POST /tasks/{id}/result or an
            # internal dispatcher failure path (_fail_if_active calls this
            # method directly) — both funnel through here. status is set
            # alongside outcome/completed_at (not left at 'in_progress') so
            # the row can't end up internally contradictory — a terminal
            # outcome sitting next to a stale in_progress status broke
            # status-based external queries (review of PR #203, finding 3).
            outcome = result.status
            if result.status == "failed" and isinstance(result.error_info, dict):
                reason = result.error_info.get("reason")
                if reason:
                    outcome = f"failed:{reason}"
            now = time.time()
            conn.execute(
                "UPDATE task_attribution SET status=?, outcome=?, completed_at=?, updated_at=? WHERE task_id=?",
                (result.status, outcome, now, now, task_id),
            )
            # #204: completed_at >= started_at is expected to always hold —
            # started_at is set once at first dispatch and never rewritten
            # (see record_attribution). It can only be violated by clock
            # skew or a corrupted row; surface that explicitly rather than
            # silently handing a downstream consumer a negative-duration
            # window.
            attr_row = conn.execute(
                "SELECT started_at FROM task_attribution WHERE task_id=?", (task_id,)
            ).fetchone()
            if attr_row is not None:
                started_at = attr_row["started_at"]
                if started_at and started_at > now:
                    logger.warning(
                        f"task_attribution timing invariant violated for {task_id!r}: "
                        f"completed_at={now} < started_at={started_at}"
                    )
            conn.commit()
            return task_type
        finally:
            conn.close()

    def requeue(self, task_id: str) -> None:
        """Roll an in_progress task back to pending so it can be dequeued again."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE tasks SET status = 'pending' WHERE task_id = ? AND status = 'in_progress'",
                (task_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def cancel(self, task_id: str) -> None:
        """Cancel a task. Dependent tasks (prev_task_id points to task_id) are marked
        'orphaned' rather than cancelled — operators can manually cancel them if desired."""
        conn = self._connect()
        try:
            conn.execute("UPDATE tasks SET status = 'cancelled' WHERE task_id = ?", (task_id,))
            # Mark pending dependents as orphaned (not cancelled) so the operator
            # can see them and decide whether to cancel or reassign.
            conn.execute(
                """
                UPDATE tasks SET status = 'orphaned'
                WHERE status IN ('pending', 'in_progress')
                AND json_extract(context, '$.prev_task_id') = ?
                """,
                (task_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def expire_stale(self, older_than_seconds: float = 600.0) -> List[str]:
        """Cancel in_progress tasks whose last_activity_at is older than
        ``older_than_seconds``. Returns list of cancelled task_ids."""
        cutoff = time.time() - older_than_seconds
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT task_id FROM tasks WHERE status = 'in_progress' AND last_activity_at < ?",
                (cutoff,),
            ).fetchall()
            task_ids = [r["task_id"] for r in rows]
            if task_ids:
                placeholders = ",".join("?" * len(task_ids))
                conn.execute(
                    f"UPDATE tasks SET status = 'cancelled' WHERE task_id IN ({placeholders})",
                    task_ids,
                )
                conn.commit()
            return task_ids
        finally:
            conn.close()

    def reset_stale_to_pending(self, older_than_seconds: float = 600.0) -> List[str]:
        """Reset in_progress tasks idle > ``older_than_seconds`` back to pending.

        Unlike ``expire_stale`` (which cancels them), this returns the tasks to
        the queue so they can be picked up again. Used by ``crew recover
        --reset-stale`` (#155).
        """
        cutoff = time.time() - older_than_seconds
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT task_id FROM tasks WHERE status = 'in_progress' AND last_activity_at < ?",
                (cutoff,),
            ).fetchall()
            task_ids = [r["task_id"] for r in rows]
            if task_ids:
                placeholders = ",".join("?" * len(task_ids))
                conn.execute(
                    f"UPDATE tasks SET status = 'pending', last_activity_at = ? "
                    f"WHERE task_id IN ({placeholders})",
                    [time.time()] + task_ids,
                )
                conn.commit()
            return task_ids
        finally:
            conn.close()

    def list_orphaned(self) -> List[TaskRequest]:
        """Return all tasks with status='orphaned'."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = 'orphaned' ORDER BY priority ASC, created_at ASC"
            ).fetchall()
            return [
                TaskRequest(
                    task_id=r["task_id"],
                    task_type=r["task_type"],
                    description=r["description"],
                    branch=r["branch"],
                    priority=r["priority"],
                    context=json.loads(r["context"]),
                    project=r["project"] if r["project"] else "",
                )
                for r in rows
            ]
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
                "UPDATE tasks SET status = 'in_progress', last_activity_at = ? WHERE task_id = ?",
                (time.time(), chosen["task_id"]),
            )
            conn.execute("COMMIT")
            return TaskRequest(
                task_id=chosen["task_id"],
                task_type=chosen["task_type"],
                description=chosen["description"],
                branch=chosen["branch"],
                priority=chosen["priority"],
                context=json.loads(chosen["context"]),
                project=chosen["project"] if chosen["project"] else "",
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

    def list_all_with_status(self) -> List[dict]:
        """Return all tasks as raw dicts including the status and project fields."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT task_id, task_type, description, branch, priority, context, status, project "
                "FROM tasks ORDER BY priority ASC, created_at ASC"
            ).fetchall()
            return [
                {
                    "task_id": r["task_id"],
                    "task_type": r["task_type"],
                    "description": r["description"],
                    "branch": r["branch"],
                    "priority": r["priority"],
                    "context": json.loads(r["context"]) if r["context"] else {},
                    "status": r["status"],
                    "project": r["project"] if r["project"] else "",
                }
                for r in rows
            ]
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
                    project=r["project"] if r["project"] else "",
                    status=r["status"],
                    # #213: these columns are already in `r` (SELECT *) —
                    # only actually meaningful once the task has a result,
                    # but harmless/empty-default otherwise.
                    summary=r["summary"] or "",
                    verdict=r["verdict"],
                    findings=json.loads(r["findings"]) if r["findings"] else [],
                    pr_number=r["pr_number"],
                    error_info=json.loads(r["error_info"]) if r["error_info"] else None,
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

    def get_task_status(self, task_id: str) -> Optional[str]:
        """Return the current DB status of a task, or None if not found (#159)."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            return row["status"] if row else None
        finally:
            conn.close()

    def bump_activity(self, task_id: str, ts: Optional[float] = None) -> None:
        """Refresh last_activity_at for a task. Called by the watchdog whenever
        the agent's pane is observed busy, so the timeout/reminder clocks
        restart from the most recent sign of life."""
        if ts is None:
            ts = time.time()
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE tasks SET last_activity_at = ? WHERE task_id = ? AND status = 'in_progress'",
                (ts, task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def set_push_at(self, task_id: str, ts: Optional[float] = None) -> None:
        """Record when push_fn was called for a task (bug #152).
        The watchdog uses push_at as the start of the idle clock so dispatch-queue
        wait time is excluded from the idle measurement."""
        if ts is None:
            ts = time.time()
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE tasks SET push_at = ? WHERE task_id = ? AND status = 'in_progress'",
                (ts, task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _reset_push_at(self, task_id: str) -> None:
        """Force push_at back to 0 (test helper — simulates MCP-dequeued tasks)."""
        conn = self._connect()
        try:
            conn.execute("UPDATE tasks SET push_at = 0 WHERE task_id = ?", (task_id,))
            conn.commit()
        finally:
            conn.close()

    def list_in_progress_with_activity(self) -> List[dict]:
        """Return dicts with the fields the watchdog needs to make timeout
        decisions: task_id, task_type, context, last_activity_at, push_at, project."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT task_id, task_type, context, last_activity_at, push_at, project "
                "FROM tasks WHERE status = 'in_progress'"
            ).fetchall()
            return [
                {
                    "task_id": r["task_id"],
                    "task_type": r["task_type"],
                    "context": json.loads(r["context"]) if r["context"] else {},
                    "last_activity_at": r["last_activity_at"] or 0.0,
                    "push_at": r["push_at"] or 0.0,
                    "project": r["project"] if r["project"] else "",
                }
                for r in rows
            ]
        finally:
            conn.close()

    def record_attribution(
        self,
        task_id: str,
        project: str = "",
        agent: str = "",
        role: str = "",
        task_type: str = "",
        worktree_path: str = "",
        repo_url: str = "",
        git_branch: str = "",
        status: str = "pending",
        # #202: durable context identity + lineage. All optional/backward
        # compatible — existing callers that only pass the fields above
        # keep working, and new rows without them default to empty/0.
        model: str = "",
        context_id: str = "",
        provider_session_id: str = "",
        context_policy: str = "",
        context_generation: int = 0,
        session_task_index: int = 0,
        previous_task_id: str = "",
        retry_of: str = "",
        fallback_of: str = "",
        started_at: float = 0.0,
    ) -> None:
        """Upsert a durable attribution record so quota systems can map token
        usage back to the project even after worktrees are torn down.

        #204: ``started_at`` is deliberately NOT in the ON CONFLICT UPDATE
        clause. A transient-error retry (#199/#205) re-dispatches the *same*
        task_id — sometimes several times — which calls this method again
        for that task_id. If started_at were overwritten on every call, each
        retry would silently erase the original first-attempt start time,
        making it impossible to distinguish real queue/retry wait time from
        execution time. The first INSERT sets it (to the caller's value, or
        `now` if the caller didn't have one — dispatch time, not queue-
        creation time); every subsequent UPSERT for that task_id leaves the
        column untouched.
        """
        codex_logs_path = (
            os.path.join(worktree_path, ".codex_local", "logs_2.sqlite")
            if agent == "codex" and worktree_path
            else ""
        )
        now = time.time()
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO task_attribution
                    (task_id, project, agent, role, task_type, worktree_path,
                     codex_logs_path, repo_url, git_branch, created_at, updated_at, status,
                     schema_version, model, context_id, provider_session_id, context_policy,
                     context_generation, session_task_index, previous_task_id, retry_of,
                     fallback_of, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    status=excluded.status, updated_at=excluded.updated_at,
                    model=excluded.model, context_id=excluded.context_id,
                    provider_session_id=excluded.provider_session_id,
                    context_policy=excluded.context_policy,
                    context_generation=excluded.context_generation,
                    session_task_index=excluded.session_task_index,
                    previous_task_id=excluded.previous_task_id,
                    retry_of=excluded.retry_of, fallback_of=excluded.fallback_of
                """,
                (task_id, project, agent, role, task_type, worktree_path,
                 codex_logs_path, repo_url, git_branch, now, now, status,
                 CONTEXT_SCHEMA_VERSION, model, context_id, provider_session_id,
                 context_policy, context_generation, session_task_index,
                 previous_task_id, retry_of, fallback_of, started_at or now),
            )
            conn.commit()
        finally:
            conn.close()

    def update_attribution_status(self, task_id: str, status: str) -> None:
        """Update the status field of an existing attribution record."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE task_attribution SET status=?, updated_at=? WHERE task_id=?",
                (status, time.time(), task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def update_attribution_outcome(self, task_id: str, outcome: str) -> None:
        """Record the final outcome + completion timestamp for a task's
        attribution row (#202). Called once a task reaches a terminal state,
        regardless of whether that happened via the agent's own result POST
        or an internal dispatcher failure path — both funnel through
        ``submit_result`` below."""
        now = time.time()
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE task_attribution SET outcome=?, completed_at=?, updated_at=? WHERE task_id=?",
                (outcome, now, now, task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_or_create_context(
        self,
        project: str,
        agent: str,
        worktree_path: str,
        role: str = "",
        task_id: str = "",
        force_reset: bool = False,
    ) -> dict:
        """Resolve the durable context identity for ``(project, agent,
        worktree_path)`` (#202).

        A context is scoped by agent+worktree, NOT role — see module-level
        design note in ``context_identity.py``. Mints a new ``context_id``
        (and bumps ``context_generation``) when no row exists yet for this
        key, or when ``force_reset=True`` (caller made an explicit
        ``context_reset`` request). Otherwise reuses the existing
        ``context_id`` and increments ``session_task_index``.

        Returns a dict: ``context_key, context_id, context_generation,
        session_task_index, context_policy`` (``"fresh"`` or ``"resume"``),
        ``previous_task_id``, ``provider_session_id``.
        """
        context_key = f"{project}::{agent}::{worktree_path}"
        now = time.time()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM context_state WHERE context_key = ?", (context_key,)
            ).fetchone()
            if row is None or force_reset:
                context_id = str(uuid.uuid4())
                generation = (row["context_generation"] + 1) if row else 1
                session_task_index = 1
                policy = "fresh"
                previous_task_id = row["last_task_id"] if row else None
                provider_session_id = None
                conn.execute(
                    """
                    INSERT INTO context_state
                        (context_key, project, role, agent, worktree_path, context_id,
                         context_generation, session_task_index, provider_session_id,
                         last_task_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(context_key) DO UPDATE SET
                        role=excluded.role, context_id=excluded.context_id,
                        context_generation=excluded.context_generation,
                        session_task_index=excluded.session_task_index,
                        provider_session_id=excluded.provider_session_id,
                        last_task_id=excluded.last_task_id, updated_at=excluded.updated_at
                    """,
                    (context_key, project, role, agent, worktree_path, context_id,
                     generation, session_task_index, provider_session_id,
                     task_id, now, now),
                )
            else:
                context_id = row["context_id"]
                generation = row["context_generation"]
                session_task_index = row["session_task_index"] + 1
                policy = "resume"
                previous_task_id = row["last_task_id"]
                provider_session_id = row["provider_session_id"]
                conn.execute(
                    """
                    UPDATE context_state
                    SET role=?, session_task_index=?, last_task_id=?, updated_at=?
                    WHERE context_key=?
                    """,
                    (role, session_task_index, task_id, now, context_key),
                )
            conn.commit()
            return {
                "context_key": context_key,
                "context_id": context_id,
                "context_generation": generation,
                "session_task_index": session_task_index,
                "context_policy": policy,
                "previous_task_id": previous_task_id,
                "provider_session_id": provider_session_id,
            }
        finally:
            conn.close()

    def update_context_provider_session_id(self, context_key: str, provider_session_id: str) -> None:
        """Record a provider-native session id observed for this context
        (#202) — e.g. parsed from claude's stream-json output. Best-effort;
        left null when the provider doesn't expose one reliably."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE context_state SET provider_session_id=?, updated_at=? WHERE context_key=?",
                (provider_session_id, time.time(), context_key),
            )
            conn.commit()
        finally:
            conn.close()

    def get_attribution(self, task_id: str) -> Optional[dict]:
        """Return the durable attribution row for ``task_id`` (#202), or
        None if no attribution was ever recorded for it. Used to correlate
        an internal dispatcher failure back to its project/role/agent/
        context_id when emitting a ``task_failed`` lifecycle event."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM task_attribution WHERE task_id = ?", (task_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def force_fail(self, task_id: str, summary: str, error_info: Optional[dict] = None) -> Optional[str]:
        """Mark an in_progress task as failed (used by the watchdog when a pane
        has been silent past the timeout). Returns the task_type so callers can
        push the next task to the now-idle role, or None if the row wasn't
        in_progress (e.g. result arrived just before the watchdog tick)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT task_type, status FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None or row["status"] != "in_progress":
                conn.execute("ROLLBACK")
                return None
            conn.execute(
                "UPDATE tasks SET status = 'failed', summary = ?, error_info = ? WHERE task_id = ?",
                (summary, json.dumps(error_info) if error_info is not None else None, task_id),
            )
            conn.execute("COMMIT")
            return row["task_type"]
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def force_fail_pending(self, task_id: str, summary: str, error_info: Optional[dict] = None) -> Optional[str]:
        """Mark a pending task as failed (#145 — MCP no-client auto-fail).

        Like force_fail but operates on pending tasks rather than in_progress ones.
        Returns the task_type, or None if the row isn't pending.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT task_type, status FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None or row["status"] != "pending":
                conn.execute("ROLLBACK")
                return None
            conn.execute(
                "UPDATE tasks SET status = 'failed', summary = ?, error_info = ? WHERE task_id = ?",
                (summary, json.dumps(error_info) if error_info is not None else None, task_id),
            )
            conn.execute("COMMIT")
            return row["task_type"]
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def list_stale_pending(self, older_than_seconds: float, now: float) -> List[dict]:
        """Return pending tasks whose created_at is more than older_than_seconds ago.

        Used by the watchdog (#136) to re-dispatch tasks that were enqueued but
        never picked up (e.g. pane was busy or push was missed).
        """
        cutoff = now - older_than_seconds
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT task_id, task_type, context, created_at FROM tasks "
                "WHERE status = 'pending' AND created_at < ?",
                (cutoff,),
            ).fetchall()
            return [
                {
                    "task_id": r["task_id"],
                    "task_type": r["task_type"],
                    "context": json.loads(r["context"]) if r["context"] else {},
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
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
