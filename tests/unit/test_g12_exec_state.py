"""G12 / D6: per-task execution state — claim, dispatch, lease, heartbeat, build.

SEV-0 alfred#51 c5777790815 §2. The tasks table had 17 columns and could not
say who claimed a task, where it went, whether it was alive, or which build
handed it out; `push_at` was 0 on 6801/6806 preserved rows (RECONCILIATION.md
F9), because only the tmux path wrote it.
"""

import asyncio
import dataclasses
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_crew import provenance
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import EXEC_STATE_COLUMNS, TaskQueue
from agent_crew.server import create_app

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pre_g12_tasks.db"
NEW_COLUMNS = [c for c in EXEC_STATE_COLUMNS if c != "push_at"]


def _columns(db):
    with sqlite3.connect(db) as c:
        return [r[1] for r in c.execute("PRAGMA table_info(tasks)")]


def _rows(db, cols):
    with sqlite3.connect(db) as c:
        return c.execute(f"SELECT {', '.join(cols)} FROM tasks ORDER BY task_id").fetchall()


def _task(task_id, task_type="implement", **kw):
    return TaskRequest(task_id=task_id, task_type=task_type, description="d",
                       branch="main", **kw)


# ---------------------------------------------------------------------------
# Migration — on a copy of a DB written by the pre-G12 code (5efea31).
# ---------------------------------------------------------------------------

@pytest.fixture
def legacy_db(tmp_path):
    db = tmp_path / "tasks.db"
    shutil.copy(FIXTURE, db)
    return str(db)


def test_fixture_is_the_pre_g12_shape(legacy_db):
    """The premise: 17 columns, as in every preserved SEV-0 DB."""
    cols = _columns(legacy_db)
    assert len(cols) == 17
    assert not set(NEW_COLUMNS) & set(cols)


def test_migration_adds_columns_and_history_without_touching_old_data(legacy_db):
    old_cols = _columns(legacy_db)
    before = _rows(legacy_db, old_cols)

    TaskQueue(legacy_db)

    cols = _columns(legacy_db)
    assert cols[:17] == old_cols                       # additive: order and names kept
    assert cols[17:] == NEW_COLUMNS
    assert _rows(legacy_db, old_cols) == before        # every old value unchanged
    # Old rows carry no invented claim: NULL, not 0 / ''.
    assert all(v is None for row in _rows(legacy_db, NEW_COLUMNS) for v in row)
    with sqlite3.connect(legacy_db) as c:
        assert c.execute("SELECT COUNT(*) FROM task_exec_events").fetchone()[0] == 0


def test_migration_is_idempotent(legacy_db):
    TaskQueue(legacy_db)
    once = (_columns(legacy_db), _rows(legacy_db, _columns(legacy_db)))
    TaskQueue(legacy_db)
    assert (_columns(legacy_db), _rows(legacy_db, _columns(legacy_db))) == once


def test_get_task_reports_legacy_rows_as_unrecorded(legacy_db):
    app = create_app(legacy_db, watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        done = client.get("/tasks/legacy-done").json()
        running = client.get("/tasks/legacy-running").json()
    # Existing fields are unchanged; `execution` is the only addition.
    assert set(done) == {f.name for f in dataclasses.fields(TaskRequest)} | {"execution"}
    assert (done["task_id"], done["task_type"], done["priority"], done["context"]["k"],
            done["project"]) == ("legacy-done", "implement", 2, "v", "legacy")
    assert all(done["execution"][c] is None for c in NEW_COLUMNS)
    assert done["execution"]["events"] == []
    assert running["execution"]["push_at"] == 1234.5   # pre-existing value surfaces
