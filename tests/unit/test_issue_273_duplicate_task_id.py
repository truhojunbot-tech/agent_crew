"""#273 review round 1 — the 409 has to mean what it says, and only that.

PR #274 made a duplicate `task_id` return 409 with the existing row's status
instead of a bare 500. Two things about it were not true yet.

**The catch was too wide.** `except sqlite3.IntegrityError` sees every
constraint on the row, not the `task_id` primary key. A NOT NULL violation on
any of the six NOT NULL columns therefore came back as "this task already
exists", with `status='unknown'` because the follow-up SELECT found nothing.
Reproduced through `TaskQueue.enqueue`:

    MISREPORTED as duplicate: task_id 'unique-1' already exists with status='unknown'

⛔That is worse than the 500 it replaced. A caller told "already exists" stops
  and treats the work as in flight; a caller told "the insert failed" retries or
  escalates. Turning a write bug into a false duplicate loses the task silently.

**The documented body was not the body.** `HTTPException(detail={...})` nests
the payload, so the response is `{"detail": {...}}` and not the top-level
`{"error", "task_id", "status"}` PR #274 described. The envelope is kept —
every other error in this server is an `HTTPException`, and a lone
`JSONResponse` would make one endpoint's error shape unique — so it is pinned
here instead, and the claim corrected rather than the code bent to it.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskAlreadyExistsError, TaskQueue
from agent_crew.server import create_app

#: Every NOT NULL column `enqueue` writes from caller-supplied data. Each one
#: could raise IntegrityError without a single duplicate being involved.
NOT_NULL_FIELDS = ["task_type", "description", "branch", "project"]


def _task(task_id="t-1", **kw):
    task = TaskRequest(task_id=task_id, task_type="review", description="d",
                       branch="b", priority=3, context={}, project="")
    for key, value in kw.items():
        # The dataclass validates; a library caller reaching the DB with a bad
        # value is exactly the case the wide catch mishandled, so build it.
        object.__setattr__(task, key, value)
    return task


# ── 1. only a duplicate task_id is a duplicate ────────────────────────


def test_a_real_duplicate_is_still_reported_as_one(tmp_db):
    """⛔The control first: narrowing the catch must not lose what #273 fixed."""
    q = TaskQueue(tmp_db)
    q.enqueue(_task("dup"))
    with pytest.raises(TaskAlreadyExistsError) as exc:
        q.enqueue(_task("dup"))
    assert exc.value.task_id == "dup" and exc.value.status == "pending"


@pytest.mark.parametrize("field", NOT_NULL_FIELDS)
def test_an_unrelated_constraint_is_not_a_duplicate(tmp_db, field):
    """★★The finding. A brand-new task_id that violates some *other* constraint
    must surface as the integrity error it is."""
    q = TaskQueue(tmp_db)
    with pytest.raises(sqlite3.IntegrityError) as exc:
        q.enqueue(_task(f"brand-new-{field}", **{field: None}))
    assert "NOT NULL" in str(exc.value)


@pytest.mark.parametrize("field", NOT_NULL_FIELDS)
def test_an_unrelated_constraint_does_not_invent_a_task(tmp_db, field):
    """⛔And it leaves nothing behind. A rolled-back insert that still reported
    "already exists" would have the caller believe a row it cannot see."""
    q = TaskQueue(tmp_db)
    with pytest.raises(sqlite3.IntegrityError):
        q.enqueue(_task(f"ghost-{field}", **{field: None}))
    assert [t.task_id for t in q.list_tasks()] == []


def test_the_duplicate_check_names_the_column_not_just_the_constraint_kind(tmp_db):
    """A UNIQUE violation on some future column is not a duplicate task_id
    either. Matching only "unique constraint failed" would misreport it the day
    such a column is added, which is precisely how this bug arrived."""
    q = TaskQueue(tmp_db)
    q.enqueue(_task("keep"))
    conn = sqlite3.connect(tmp_db)
    conn.execute("CREATE UNIQUE INDEX ux_probe ON tasks(branch) WHERE branch = 'unique-branch'")
    conn.commit()
    conn.close()

    q.enqueue(_task("first", branch="unique-branch"))
    with pytest.raises(sqlite3.IntegrityError) as exc:
        q.enqueue(_task("second", branch="unique-branch"))
    assert "UNIQUE" in str(exc.value)


def test_a_duplicate_still_reports_the_live_status(tmp_db):
    q = TaskQueue(tmp_db)
    q.enqueue(_task("done-1"))
    q.submit_result("done-1", TaskResult(task_id="done-1", status="completed", summary="s"))
    with pytest.raises(TaskAlreadyExistsError) as exc:
        q.enqueue(_task("done-1"))
    assert exc.value.status == "completed"


def test_the_predicate_requires_the_constraint_kind_as_well_as_the_column():
    """⛔Unit-level, on synthesised messages, and deliberately so.

    This schema cannot currently produce a non-UNIQUE IntegrityError that names
    `tasks.task_id` — SQLite lets a PRIMARY KEY column hold NULL in a rowid
    table, and there is no CHECK or FOREIGN KEY on it — so the column half of
    the predicate is the only half any end-to-end test can exercise. Mutation
    confirmed that: dropping the constraint-kind check killed nothing.

    The kind check still earns its place, because the day someone adds
    `CHECK (length(task_id) > 0)` a violation would otherwise be reported as
    "this task already exists" — the same mistake as catching every
    IntegrityError, one constraint later. Messages below are SQLite's
    documented wording; the test pins the predicate, not the driver.
    """
    from agent_crew.queue import _is_duplicate_task_id

    assert _is_duplicate_task_id(
        sqlite3.IntegrityError("UNIQUE constraint failed: tasks.task_id"))
    for other in (
        "CHECK constraint failed: tasks.task_id",
        "NOT NULL constraint failed: tasks.task_id",
        "FOREIGN KEY constraint failed",
        "UNIQUE constraint failed: tasks.branch",
        "UNIQUE constraint failed: pr_announcements.pr_number",
    ):
        assert not _is_duplicate_task_id(sqlite3.IntegrityError(other)), other


# ── 2. the 409's actual shape ─────────────────────────────────────────


def _client(tmp_db):
    return TestClient(create_app(db_path=tmp_db, watchdog_disabled=True,
                                 anomaly_disabled=True, push_fn=lambda *a, **k: None))


def _post(c, task_id):
    return c.post("/tasks", json={"task_id": task_id, "task_type": "review",
                                  "description": "d", "branch": "b", "priority": 3,
                                  "context": {}, "project": ""})


def test_the_409_body_is_the_detail_envelope_exactly(tmp_db):
    """★★The contract, pinned to the real bytes on the wire.

    PR #274 documented a top-level `{"error", "task_id", "status"}`. FastAPI
    nests an `HTTPException`'s payload, so a client coding to that description
    reads `None` for every field. The envelope stays — consistency with every
    other error this server returns — and the assertion is on the whole body so
    it cannot drift back into fiction."""
    with _client(tmp_db) as c:
        _post(c, "dup-http")
        r = _post(c, "dup-http")

    assert r.status_code == 409
    assert r.json() == {"detail": {"error": "task_id already exists",
                                   "task_id": "dup-http", "status": "pending"}}


def test_the_409_reports_the_status_the_existing_task_is_in(tmp_db):
    """The point of the whole change: a caller has to be able to tell "already
    running" from "already finished" without a second request."""
    with _client(tmp_db) as c:
        _post(c, "dup-status")
        TaskQueue(tmp_db).submit_result(
            "dup-status", TaskResult(task_id="dup-status", status="completed", summary="s"))
        assert _post(c, "dup-status").json()["detail"]["status"] == "completed"


def test_the_first_post_is_untouched_by_the_second(tmp_db):
    with _client(tmp_db) as c:
        assert _post(c, "once").status_code == 201
        _post(c, "once")
    assert len(TaskQueue(tmp_db).list_tasks()) == 1
