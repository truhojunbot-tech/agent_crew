"""Queue stale-pending behavior."""
import time
from unittest.mock import MagicMock, call, patch


# ---------------------------------------------------------------------------
# _pane_token_count  (#133)
# ---------------------------------------------------------------------------


def _fake_capture(stdout: str):
    m = MagicMock()
    m.stdout = stdout
    return m












# ---------------------------------------------------------------------------
# _pane_clear_context  (#133)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# _pane_dismiss_permission_prompt  (#134)
# ---------------------------------------------------------------------------


_GEMINI_PROMPT = (
    "Allow execution of tools requested by this agent?\n"
    "  1) No\n  2) Yes, allow for this session\n"
)








# ---------------------------------------------------------------------------
# list_stale_pending  (#136)  — queue method test
# ---------------------------------------------------------------------------


def test_list_stale_pending_returns_old_tasks(tmp_db):
    """Pending tasks older than the threshold are returned."""
    from agent_crew.queue import TaskQueue

    q = TaskQueue(tmp_db)
    old_ts = time.time() - 200  # 200s ago
    from agent_crew.protocol import TaskRequest

    req = TaskRequest(
        task_id="stale-1",
        task_type="implement",
        description="old work",
        branch="main",
    )
    # Enqueue normally then update created_at to be old.
    q.enqueue(req)
    conn = q._connect()
    conn.execute("UPDATE tasks SET created_at = ? WHERE task_id = ?", (old_ts, "stale-1"))
    conn.commit()
    conn.close()

    stale = q.list_stale_pending(older_than_seconds=120, now=time.time())
    assert any(r["task_id"] == "stale-1" for r in stale)


def test_list_stale_pending_excludes_fresh_tasks(tmp_db):
    """Tasks enqueued recently are NOT returned as stale."""
    from agent_crew.queue import TaskQueue
    from agent_crew.protocol import TaskRequest

    q = TaskQueue(tmp_db)
    req = TaskRequest(
        task_id="fresh-1",
        task_type="implement",
        description="new work",
        branch="main",
    )
    q.enqueue(req)

    stale = q.list_stale_pending(older_than_seconds=120, now=time.time())
    assert not any(r["task_id"] == "fresh-1" for r in stale)


def test_list_stale_pending_excludes_in_progress(tmp_db):
    """in_progress tasks are never returned (only pending)."""
    from agent_crew.queue import TaskQueue
    from agent_crew.protocol import TaskRequest

    q = TaskQueue(tmp_db)
    old_ts = time.time() - 500

    req = TaskRequest(
        task_id="prog-1",
        task_type="implement",
        description="in progress work",
        branch="main",
    )
    q.enqueue(req)
    conn = q._connect()
    conn.execute(
        "UPDATE tasks SET status = 'in_progress', created_at = ? WHERE task_id = ?",
        (old_ts, "prog-1"),
    )
    conn.commit()
    conn.close()

    stale = q.list_stale_pending(older_than_seconds=120, now=time.time())
    assert not any(r["task_id"] == "prog-1" for r in stale)
