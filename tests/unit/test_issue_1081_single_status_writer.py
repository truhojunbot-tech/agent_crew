"""#1081 slice a: one transition table, one status writer.

The guard scans ``src/`` for SQL that writes ``tasks.status`` anywhere except
``task_state.py`` and compares it with an explicit allowlist of today's
writers (file + function, with a count, never line numbers). A new writer
fails this test; a writer moved into :func:`task_state.transition` must be
removed from the allowlist in the same PR. Later slices shrink it to empty.
"""

from __future__ import annotations

import ast
import json
import random
import re
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from agent_crew import queue as queue_mod
from agent_crew import task_state as ts
from agent_crew.queue import TaskQueue
from agent_crew.protocol import TaskRequest

SRC = Path(__file__).resolve().parents[2] / "src"

#: Today's direct ``tasks.status`` writers outside task_state.py:
#: (file relative to src/agent_crew, function) -> number of SQL sites.
ALLOWLIST = Counter({
    ("queue.py", "_migrate_legacy_task_rows"): 1,
    ("queue.py", "record_prepared_review_base"): 1,
    ("queue.py", "claim_through_gate"): 1,
    ("queue.py", "bind_independent_reviews"): 1,
    ("queue.py", "_block_one_independent_review"): 1,
    ("queue.py", "mark_needs_human"): 1,
    ("queue.py", "submit_result"): 1,
    ("queue.py", "defer_push_delivery"): 1,
    ("queue.py", "defer_prep_retry"): 1,
    ("queue.py", "defer_provider_capacity"): 1,
    ("queue.py", "requeue"): 1,
    ("queue.py", "park_codex_quota"): 1,
    ("queue.py", "resume_codex_quota"): 1,
    ("queue.py", "park_pool_hold"): 1,
    ("queue.py", "resume_pool_hold"): 1,
    ("queue.py", "requeue_dispatcher_claim"): 1,
    ("queue.py", "reset_stale_to_pending"): 1,
    ("queue.py", "force_fail"): 1,
    ("queue.py", "force_fail_pending"): 1,
})

_UPDATE_TASKS = re.compile(r"UPDATE\s+tasks\s+SET\b(?P<set>.*?)(?:\bWHERE\b|$)", re.I | re.S)
_STATUS_ASSIGN = re.compile(r"(?<![\w.])status\s*=", re.I)
_INSERT_TASKS_UPSERT = re.compile(
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+tasks\b.*\bDO\s+UPDATE\s+SET\b(?P<set>.*)", re.I | re.S)


def _sql_pieces(node: ast.AST) -> str:
    """Every string literal under ``node``, joined — covers implicit and ``+``
    concatenation and the literal parts of f-strings."""
    return " ".join(n.value for n in ast.walk(node)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str))


def _writes_status(sql: str) -> int:
    hits = sum(1 for m in _UPDATE_TASKS.finditer(sql) if _STATUS_ASSIGN.search(m.group("set")))
    hits += sum(1 for m in _INSERT_TASKS_UPSERT.finditer(sql)
                if _STATUS_ASSIGN.search(m.group("set")))
    return hits


def _scan(root: Path = SRC) -> Counter:
    found: Counter = Counter()
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root / "agent_crew").as_posix() if (root / "agent_crew") in path.parents else path.as_posix()
        if rel == "task_state.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner: dict = {}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for n in ast.walk(fn):
                    owner[id(n)] = fn.name  # innermost wins: inner defs walk later
        seen: set = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and n.args:
                target = n.args[0]
            elif isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
                target = n.value
            else:
                continue
            if id(target) in seen:
                continue
            for sub in ast.walk(target):
                seen.add(id(sub))
            hits = _writes_status(_sql_pieces(target))
            if hits:
                found[(rel, owner.get(id(n), "<module>"))] += hits
    return found


def test_no_status_writer_outside_the_allowlist():
    found = _scan()
    new = found - ALLOWLIST
    gone = ALLOWLIST - found
    assert not new, (
        f"new direct tasks.status writer(s) {dict(new)}: route them through "
        "agent_crew.task_state.transition() instead (#1081)")
    assert not gone, (
        f"allowlisted writer(s) {dict(gone)} no longer write status directly: "
        "remove them from ALLOWLIST so the list only shrinks (#1081)")


def test_scanner_catches_a_new_writer(tmp_path):
    pkg = tmp_path / "agent_crew"
    pkg.mkdir()
    (pkg / "rogue.py").write_text(
        "def sneak(conn):\n"
        "    conn.execute(\"UPDATE tasks SET summary=?, \"\n"
        "                 \"status='completed' WHERE task_id=?\", ('x', 't'))\n"
        "SQL = 'UPDATE tasks SET status = ? WHERE task_id = ?'\n"
        "def harmless(conn):\n"
        "    conn.execute('UPDATE tasks SET status_changed_at=? WHERE task_id=?')\n"
        "    conn.execute('UPDATE gates SET status=? WHERE id=?')\n"
        "    conn.execute('UPDATE tasks SET context=? WHERE task_id=? AND status=?')\n")
    assert _scan(tmp_path) == Counter({("rogue.py", "sneak"): 1,
                                       ("rogue.py", "<module>"): 1})


def test_every_writer_is_a_table_row():
    """Each allowlisted (unrouted) writer performs at least one table event,
    and each unrouted event names only allowlisted writers."""
    table_writers = {(w.split(":")[0], w.split(":")[1])
                     for t in ts.TABLE.values() if not t.routed for w in t.writers}
    assert table_writers == set(ALLOWLIST)
    for t in ts.TABLE.values():
        assert t.from_statuses <= ts.STATUSES, t.event
        assert t.to_statuses and t.to_statuses <= ts.STATUSES, t.event
        assert "status" not in t.set_columns, t.event


def test_terminal_set_matches_queue():
    assert ts.TERMINAL_STATUSES == queue_mod.TERMINAL_TASK_STATUSES


def test_reopening_rows_are_explicit():
    reopening = {t.event for t in ts.TABLE.values()
                 if any(f in ts.TERMINAL_STATUSES for f in t.from_statuses)
                 and any(s not in ts.TERMINAL_STATUSES for s in t.to_statuses)}
    assert reopening == {t.event for t in ts.TABLE.values() if t.reopens_terminal}


# ── transition() ─────────────────────────────────────────────────────────

def _mem_db():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
                 "summary TEXT, context TEXT NOT NULL DEFAULT '{}', claim_source TEXT)")
    conn.execute(queue_mod._DDL_TASK_EXEC_EVENTS)
    return conn


def _events(conn, task_id):
    return [(r["event"], json.loads(r["fields"])) for r in conn.execute(
        "SELECT event, fields FROM task_exec_events WHERE task_id=? ORDER BY event_id",
        (task_id,))]


def test_transition_cas_success_writes_fields_and_no_event():
    conn = _mem_db()
    conn.execute("INSERT INTO tasks (task_id, status) VALUES ('t', 'needs_human')")
    out = ts.transition(conn, "t", "owner_readmission_retire", ("needs_human",),
                        {"summary": "done"})
    assert out.ok and out.from_status == "needs_human" and out.to_status == "cancelled"
    row = conn.execute("SELECT status, summary FROM tasks").fetchone()
    assert (row["status"], row["summary"]) == ("cancelled", "done")
    assert _events(conn, "t") == []


def test_transition_stale_when_row_moved_to_another_legal_source():
    conn = _mem_db()
    conn.execute("INSERT INTO tasks (task_id, status) VALUES ('t', 'in_progress')")
    out = ts.transition(conn, "t", "cancel", ("pending",))
    assert not out and out.refusal == ts.STALE_TRANSITION and out.from_status == "in_progress"
    assert conn.execute("SELECT status FROM tasks").fetchone()[0] == "in_progress"
    [(kind, fields)] = _events(conn, "t")
    assert kind == "stale_transition"
    assert fields["transition_event"] == "cancel" and fields["actual_status"] == "in_progress"


def test_transition_stale_when_row_missing():
    conn = _mem_db()
    out = ts.transition(conn, "gone", "cancel", ("pending",))
    assert out.refusal == ts.STALE_TRANSITION and out.reason == "task_missing"


@pytest.mark.parametrize("event,expect,fields,to,reason", [
    ("no_such_event", ("pending",), None, None, "unknown_event"),
    ("cancel", ("completed",), None, None, "expect_from_not_allowed"),
    ("cancel", ("pending",), None, "failed", "target_not_allowed"),
    ("cancel", ("pending",), {"summary": "x"}, None, "undeclared_column:summary"),
    ("cancel", ("pending",), {"status": "x"}, None, "undeclared_column:status"),
    ("result", ("pending",), None, None, "ambiguous_target"),
])
def test_transition_illegal_requests_refused_without_writing(event, expect, fields, to, reason):
    conn = _mem_db()
    conn.execute("INSERT INTO tasks (task_id, status) VALUES ('t', 'pending')")
    out = ts.transition(conn, "t", event, expect, fields, to_status=to)
    assert out.refusal == ts.ILLEGAL_TRANSITION and out.reason == reason
    assert conn.execute("SELECT status FROM tasks").fetchone()[0] == "pending"
    assert [k for k, _ in _events(conn, "t")] == ["illegal_transition"]


def test_transition_illegal_when_actual_status_cannot_start_event():
    conn = _mem_db()
    conn.execute("INSERT INTO tasks (task_id, status) VALUES ('t', 'completed')")
    out = ts.transition(conn, "t", "cancel", ("pending",))
    assert out.refusal == ts.ILLEGAL_TRANSITION and out.reason == "from_not_allowed"
    assert conn.execute("SELECT status FROM tasks").fetchone()[0] == "completed"


def test_transition_does_not_raise_when_event_log_is_missing():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, status TEXT)")
    out = ts.transition(conn, "t", "cancel", ("pending",))
    assert out.refusal == ts.STALE_TRANSITION


# ── routed cancel group, through the real queue ─────────────────────────

def _queue(tmp_path, monkeypatch):
    for k in ("AGENT_CREW_CEA_MODE", "AGENT_CREW_DB", "AGENT_CREW_DISPATCHER"):
        monkeypatch.delenv(k, raising=False)
    return TaskQueue(str(tmp_path / "tasks.db"))


def test_cancel_routes_through_transition(tmp_path, monkeypatch):
    q = _queue(tmp_path, monkeypatch)
    parent = q.enqueue(TaskRequest(task_id="p1081", task_type="implement",
                                   description="p", branch="agent/p"))
    child = q.enqueue(TaskRequest(task_id="c1081", task_type="review", description="c",
                                  branch="agent/p", context={"prev_task_id": parent}))
    calls = []
    real = ts.transition
    monkeypatch.setattr(ts, "transition",
                        lambda conn, tid, ev, *a, **k: calls.append((tid, ev)) or real(conn, tid, ev, *a, **k))
    assert q.cancel(parent) is True
    assert calls == [(parent, "cancel"), (child, "orphan_dependent")]
    assert getattr(q.get_task(parent), "status", None) == "cancelled"
    assert getattr(q.get_task(child), "status", None) == "orphaned"
    assert q.cancel(parent) is False  # terminal: refused before any write


# ── property scaffold: seeded random walk over the table ────────────────

RUNS = 1000
STEPS = 25


def test_random_event_walk_never_reopens_a_terminal_row_implicitly():
    rng = random.Random(1081)
    events = sorted(ts.TABLE)
    conn = _mem_db()
    for run in range(RUNS):
        task_id = f"t{run}"
        status = rng.choice(sorted(ts.STATUSES))
        conn.execute("INSERT INTO tasks (task_id, status) VALUES (?, ?)", (task_id, status))
        refusals = 0
        for _ in range(STEPS):
            event = rng.choice(events)
            spec = ts.TABLE[event]
            # A caller usually expects the true status, sometimes a stale one.
            expect = status if rng.random() < 0.8 else rng.choice(sorted(ts.STATUSES))
            to = rng.choice(sorted(spec.to_statuses))
            out = ts.transition(conn, task_id, event, (expect,), to_status=to)
            stored = conn.execute("SELECT status FROM tasks WHERE task_id=?",
                                  (task_id,)).fetchone()[0]
            if out.ok:
                assert spec.allows(status, to) and expect == status
                if status in ts.TERMINAL_STATUSES and to not in ts.TERMINAL_STATUSES:
                    assert spec.reopens_terminal, (run, status, event, to)
                status = to
            else:
                refusals += 1
                assert out.refusal in (ts.ILLEGAL_TRANSITION, ts.STALE_TRANSITION)
                legal = spec.allows(status, to) and expect == status
                assert not legal, (run, status, event, expect, to)
            assert stored == status
        assert len(_events(conn, task_id)) == refusals


def test_transition_propagates_database_errors_like_the_old_writer():
    conn = _mem_db()
    conn.execute("INSERT INTO tasks (task_id, status) VALUES ('t', 'pending')")
    conn.execute("CREATE TRIGGER boom BEFORE UPDATE OF status ON tasks "
                 "BEGIN SELECT RAISE(ABORT, 'boom'); END")
    with pytest.raises(sqlite3.IntegrityError):
        ts.transition(conn, "t", "cancel", ("pending",))
