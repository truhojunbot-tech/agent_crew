"""#1081 slice a: one transition table, one status writer.

The guard compiles every SQL write literal under ``src/`` (except
``task_state.py``) against the repo schema, lets SQLite's authorizer say which
ones write ``tasks.status``, and compares it with an explicit allowlist of today's
writers (file + function, with a count, never line numbers). A new writer
fails this test; a writer moved into :func:`task_state.transition` must be
removed from the allowlist in the same PR. Later slices shrink it to empty.
"""

from __future__ import annotations

import ast
import itertools
import json
import os
import random
import re
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from agent_crew import queue as queue_mod
from agent_crew import task_state as ts
from agent_crew.cea import store as cea_store
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

# ── the guard: SQLite decides what a statement writes ───────────────────
#
# Three rounds of regex were bypassed by ordinary SQL (comments, a quoted
# 'WHERE', a subquery in SET). So the guard does not read SQL text: it lets
# SQLite compile each candidate statement against the repo's own schema with
# an authorizer that records SQLITE_UPDATE on tasks.status. Compile only —
# statements run under EXPLAIN, which never executes them, on a throwaway
# in-memory copy of the schema.

#: Stands in for a non-literal fragment (an f-string field, a ``+``'d name).
_HOLE = "\x01"
#: What a hole is tried as, in order. Its content is unknowable statically, so
#: the guard sees only the literal parts; it needs the statement to compile.
_FILLERS = ("rowid", "NULL", "rowid=rowid", "", ", rowid=rowid")
_MAX_HOLES = 3
#: Which literals are SQL writes at all. Deliberately loose: a false candidate
#: (prose, ``update-ref``) fails to compile and fails the guard loudly, while a
#: missed one would pass silently. Excluded only: a bare word, a word that
#: continues (``updated``), a lone ``-``.
_WRITE_HEAD = re.compile(r"(?:UPDATE|INSERT|REPLACE|WITH)(?![\w$]|-(?!-))\s*\S", re.I)
_DDL_HEAD = re.compile(r"(?:CREATE|ALTER)\b", re.I)
_LEADING = re.compile(r"(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?(?:\*/|\Z))*", re.S)
_BINDINGS = re.compile(r"The current statement uses (\d+)")
_NO_FUNCTION = re.compile(r"no such function: (\w+)$")


class Unpreparable(Exception):
    pass


def _fold(e: ast.AST):
    """The string an expression builds, with a hole per non-literal part, or
    None if it is not a string expression at all."""
    if isinstance(e, ast.Constant) and isinstance(e.value, str):
        return e.value
    if isinstance(e, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else _HOLE for v in e.values)
    if isinstance(e, ast.BinOp) and isinstance(e.op, ast.Add):
        left, right = _fold(e.left), _fold(e.right)
        if left is None and right is None:
            return None
        return (_HOLE if left is None else left) + (_HOLE if right is None else right)
    return None


def _string_exprs(tree: ast.AST):
    """(node, text) for every outermost string expression; docstrings skipped."""
    inner: set = set()
    for n in ast.walk(tree):
        if id(n) in inner:
            continue
        if isinstance(n, ast.Expr) and _fold(n.value) is not None:
            inner.update(id(sub) for sub in ast.walk(n.value))
            continue
        text = _fold(n)
        if text is None:
            continue
        inner.update(id(sub) for sub in ast.walk(n))
        yield n, text


def _statements(text: str):
    """Split on ``;`` where SQLite's own tokenizer says a statement ends."""
    buf = ""
    for ch in text:
        buf += ch
        if ch == ";" and sqlite3.complete_statement(buf):
            yield buf
            buf = ""
    if buf.strip():
        yield buf


def _head(stmt: str) -> str:
    return stmt[_LEADING.match(stmt).end():]


def _allow_all(*_):
    return sqlite3.SQLITE_OK


def _status_writes(conn: sqlite3.Connection, stmt: str) -> tuple:
    """(own, triggers): whether compiling ``stmt`` writes tasks.status itself,
    and the names of triggers it fires that do. Raises Unpreparable."""
    holes = stmt.count(_HOLE)
    if holes > _MAX_HOLES:
        raise Unpreparable(f"{holes} non-literal fragments")
    first_columns = tuple(
        cols[0][1] for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if (cols := conn.execute(f"PRAGMA table_info({name!r})").fetchall()))
    error = None
    for fill in itertools.product(_FILLERS + first_columns, repeat=holes):
        sql = stmt
        for f in fill:
            sql = sql.replace(_HOLE, f, 1)
        writes, inserts = [], []

        def auth(action, arg1, arg2, _db, trigger):
            table = (arg1 or "").lower()
            if action == sqlite3.SQLITE_UPDATE and table == "tasks" and (arg2 or "").lower() == "status":
                writes.append(trigger)
            elif action == sqlite3.SQLITE_INSERT and table == "tasks" and trigger is None:
                inserts.append(True)
            return sqlite3.SQLITE_OK

        conn.set_authorizer(auth)
        try:
            try:
                program = conn.execute("EXPLAIN " + sql).fetchall()
            except sqlite3.ProgrammingError as e:
                m = _BINDINGS.search(str(e))
                if not m:
                    raise
                program = conn.execute("EXPLAIN " + sql, (None,) * int(m.group(1))).fetchall()
        except sqlite3.OperationalError as e:
            m = _NO_FUNCTION.match(str(e))
            if m:  # an application-defined SQL function: any stub compiles
                conn.create_function(m.group(1), -1, lambda *a: None)
                return _status_writes(conn, stmt)
            error = error or e
            continue
        except sqlite3.Error as e:
            error = error or e
            continue
        finally:
            conn.set_authorizer(_allow_all)  # 3.10 cannot clear an authorizer
        # INSERT creates a row; REPLACE (a Delete in the program) overwrites one.
        replaces = bool(inserts) and any(op[1] == "Delete" for op in program)
        return (None in writes or replaces), {t for t in writes if t is not None}
    raise Unpreparable(str(error))


def _schema_db(tmp_dir: Path) -> sqlite3.Connection:
    """In-memory copy of the repo's schema: what TaskQueue and the CEA store
    create (their migrations build columns dynamically), then every literal
    CREATE/ALTER under src/ until a fixpoint, for the other modules' tables."""
    path = tmp_dir / "schema.db"
    TaskQueue(str(path))
    disk = sqlite3.connect(path)
    cea_store.ensure_schema(disk)
    disk.commit()
    conn = sqlite3.connect(":memory:", cached_statements=0)  # a cached statement skips the authorizer
    disk.backup(conn)
    disk.close()
    pending = [st for p in sorted(SRC.rglob("*.py"))
               for _, text in _string_exprs(ast.parse(p.read_text(encoding="utf-8")))
               if _HOLE not in text
               for st in _statements(text) if _DDL_HEAD.match(_head(st))]
    while pending:
        left = []
        for st in pending:
            try:
                conn.execute(st)
            except sqlite3.Error:
                left.append(st)
        if len(left) == len(pending):
            break
        pending = left
    return conn


@pytest.fixture(scope="module")
def schema(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        for k in [k for k in os.environ if k.startswith("AGENT_CREW_")]:
            mp.delenv(k)
        conn = _schema_db(tmp_path_factory.mktemp("schema1081"))
    yield conn
    conn.close()


@pytest.fixture
def fixture_db(schema):
    """The repo schema plus the unrelated tables the SQL fixtures name."""
    conn = sqlite3.connect(":memory:", cached_statements=0)
    schema.backup(conn)
    for ddl in ("CREATE TABLE subtasks (id, status)", "CREATE TABLE tasks_archive (id, status)",
                "CREATE TABLE t (id, name)", "CREATE TABLE other (id, name)"):
        conn.execute(ddl)
    return conn


def _writes_status(conn: sqlite3.Connection, sql: str) -> int:
    hits = 0
    for st in _statements(sql):
        if _WRITE_HEAD.match(_head(st)):
            own, triggers = _status_writes(conn, st)
            hits += own + len(triggers)
    return hits


@pytest.mark.parametrize("sql", [
    "UPDATE tasks SET status = ? WHERE task_id = ?",
    'UPDATE "tasks" SET "status" = ? WHERE task_id = ?',
    "UPDATE `tasks` SET `status`=? WHERE task_id=?",
    "UPDATE [tasks] SET [status]=? WHERE task_id=?",
    "UPDATE 'tasks' SET 'status'=? WHERE task_id=?",
    "UPDATE main.tasks SET status=? WHERE task_id=?",
    'UPDATE "main"."tasks" SET "status"=? WHERE task_id=?',
    "UPDATE main . [tasks] SET status=?",
    "UPDATE OR IGNORE tasks SET status=? WHERE task_id=?",
    "update Tasks set Status=? where task_id=?",
    "UPDATE tasks AS t SET status=? WHERE t.task_id=?",
    "UPDATE tasks NOT INDEXED SET status=?",
    "UPDATE tasks SET summary=?, (status, verdict) = (?, ?) WHERE task_id=?",
    "UPDATE tasks SET (summary, \"status\") = (?, ?)",
    "INSERT INTO main.\"tasks\" (task_id, task_type, description, created_at) VALUES (?, ?, ?, ?) "
    "ON CONFLICT(task_id) DO UPDATE SET \"status\"=excluded.status",
    "REPLACE INTO tasks (task_id, task_type, description, created_at, status) VALUES (?, ?, ?, ?, ?)",
    "INSERT OR REPLACE INTO tasks (task_id, task_type, description, created_at) VALUES (?, ?, ?, ?)",
    "UPDATE tasks SET status=:status WHERE task_id=:task_id",
    "WITH x(v) AS (SELECT 'completed') UPDATE tasks SET status=(SELECT v FROM x)",
    # A comment is whitespace to SQLite, so it is to the guard too.
    "UPDATE tasks /* migration note */ SET status=? WHERE task_id=?",
    "UPDATE tasks SET status /* x */ = ? WHERE task_id=?",
    "UPDATE tasks -- note\nSET status=? WHERE task_id=?",
    "UPDATE tasks SET status -- why\n= ? WHERE task_id=?",
    "UPDATE/**/tasks/**/SET/**/status/**/=?",
    "/* lead */ UPDATE tasks SET status=?",
    "UPDATE--c\ntasks SET status=?",
    "UPDATE main./* q */tasks AS t /* a */ SET summary=?, \"status\"/**/=?",
    "UPDATE tasks SET summary='--', status=? WHERE task_id=?",
    "UPDATE tasks SET summary='/*', status=? WHERE summary='*/'",
    # Round 3: clause keywords inside a literal or a subquery are not clauses.
    "UPDATE tasks SET summary='WHERE', status=? WHERE task_id=?",
    "UPDATE tasks SET summary='WHERE', status='completed' WHERE task_id=?",
    "UPDATE tasks SET summary=(SELECT name FROM t WHERE id=1), status=?",
    "UPDATE tasks SET summary=(SELECT name FROM other WHERE id=1), status='completed' WHERE task_id=?",
])
def test_writes_status_sees_every_sqlite_spelling(fixture_db, sql):
    assert _writes_status(fixture_db, sql) == 1, sql


def test_every_statement_in_a_literal_is_compiled(fixture_db):
    assert _writes_status(fixture_db, "UPDATE tasks SET summary=? ; UPDATE tasks SET status=?") == 1
    assert _writes_status(fixture_db, "UPDATE tasks SET summary='a;b'; UPDATE tasks SET status=?;"
                                      "UPDATE tasks SET status=?") == 2


@pytest.mark.parametrize("sql", [
    "UPDATE tasks SET status_changed_at=? WHERE task_id=?",
    'UPDATE "tasks" SET "status_changed_at"=? WHERE task_id=?',
    "UPDATE tasks SET context=? WHERE task_id=? AND status=?",
    'UPDATE "tasks" SET context=? WHERE "status"=?',
    "UPDATE gates SET status=? WHERE rowid=?",
    'UPDATE "task_attribution" SET "status"=? WHERE task_id=?',
    "UPDATE main.tasks_archive SET status=?",
    "UPDATE subtasks SET status=?",
    "UPDATE tasks SET (summary, verdict) = (?, ?) WHERE status=?",
    "INSERT INTO tasks (task_id, task_type, description, created_at, status) VALUES (?, ?, ?, ?, ?)",
    "INSERT OR IGNORE INTO tasks (task_id, task_type, description, created_at) VALUES (?, ?, ?, ?)",
    "UPDATE tasks SET summary=? /* status = ? */ WHERE task_id=?",
    "UPDATE tasks SET summary=? -- , status=?\nWHERE task_id=?",
    # status text only inside a literal value
    "UPDATE tasks SET summary='status = ?' WHERE task_id=?",
    "UPDATE tasks SET summary='x'', status=''completed' WHERE task_id=?",
    "UPDATE tasks SET summary=\"status\" WHERE task_id=?",
    "UPDATE subtasks SET status=(SELECT status FROM tasks WHERE task_id=?)",
    "WITH x AS (SELECT status FROM tasks) UPDATE subtasks SET status=(SELECT status FROM x)",
])
def test_writes_status_ignores_non_status_writes(fixture_db, sql):
    assert _writes_status(fixture_db, sql) == 0, sql


def test_unpreparable_write_is_a_guard_failure_not_a_pass(fixture_db):
    with pytest.raises(Unpreparable):
        _writes_status(fixture_db, "UPDATE tasks SET no_such_column=? WHERE task_id=?")
    with pytest.raises(Unpreparable):
        _writes_status(fixture_db, "UPDATE temp.tasks SET status=?")


def _scan(conn: sqlite3.Connection, root: Path = SRC) -> tuple:
    """(writers, unpreparable, triggers) for every .py under ``root``."""
    found: Counter = Counter()
    unpreparable: list = []
    triggers: set = set()
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
        for node, text in _string_exprs(tree):
            for st in _statements(text):
                if not _WRITE_HEAD.match(_head(st)):
                    continue
                try:
                    own, fired = _status_writes(conn, st)
                except Unpreparable as e:
                    unpreparable.append(f"{rel}:{node.lineno}: {e}: {st.strip()[:120]!r}")
                    continue
                if own:
                    found[(rel, owner.get(id(node), "<module>"))] += 1
                triggers |= fired
    return found, unpreparable, triggers


def test_no_status_writer_outside_the_allowlist(schema):
    found, unpreparable, triggers = _scan(schema)
    assert not unpreparable, (
        "SQL the guard cannot compile against the repo schema, so cannot vouch "
        f"for: {unpreparable}")
    assert not triggers, f"trigger(s) {sorted(triggers)} write tasks.status (#1081)"
    new = found - ALLOWLIST
    gone = ALLOWLIST - found
    assert not new, (
        f"new direct tasks.status writer(s) {dict(new)}: route them through "
        "agent_crew.task_state.transition() instead (#1081)")
    assert not gone, (
        f"allowlisted writer(s) {dict(gone)} no longer write status directly: "
        "remove them from ALLOWLIST so the list only shrinks (#1081)")


def test_scanner_catches_a_new_writer(schema, tmp_path):
    pkg = tmp_path / "agent_crew"
    pkg.mkdir()
    (pkg / "rogue.py").write_text(
        '"""Update the status of tasks — a docstring, never executed."""\n'
        "def sneak(conn):\n"
        "    conn.execute(\"UPDATE tasks SET summary=?, \"\n"
        "                 \"status='completed' WHERE task_id=?\", ('x', 't'))\n"
        "SQL = 'UPDATE tasks SET status = ? WHERE task_id = ?'\n"
        "def harmless(conn):\n"
        "    conn.execute('UPDATE tasks SET summary=? WHERE task_id=?')\n"
        "    conn.execute('UPDATE gates SET status=? WHERE rowid=?')\n"
        "    conn.execute('UPDATE tasks SET context=? WHERE task_id=? AND status=?')\n"
        "    conn.execute(\"UPDATE tasks SET summary='status=1' WHERE task_id=?\")\n"
        "def quoted(conn):\n"
        "    conn.execute('UPDATE \"tasks\" SET \"status\" = ? WHERE task_id = ?')\n"
        "    conn.execute(f'UPDATE main.[tasks] SET [status] = {1} WHERE task_id = ?')\n"
        "def commented(conn):\n"
        "    conn.execute('UPDATE tasks /* migration note */ SET status=? WHERE task_id=?')\n"
        "    conn.execute('UPDATE tasks SET status /* x */ = ? WHERE task_id=?')\n"
        "def clause_words(conn):\n"
        "    conn.execute(\"UPDATE tasks SET summary='WHERE', status='completed' WHERE task_id=?\")\n"
        "    conn.execute('UPDATE tasks SET summary=(SELECT branch FROM tasks WHERE rowid=1), '\n"
        "                 \"status='completed'\")\n"
        "def dynamic(conn, cols):\n"
        "    conn.execute('UPDATE tasks SET ' + cols + ', status=? WHERE task_id=?')\n"
        "def broken(conn):\n"
        "    conn.execute('UPDATE tasks SET no_such_column=? WHERE task_id=?')\n")
    found, unpreparable, triggers = _scan(schema, tmp_path)
    assert found == Counter({("rogue.py", "sneak"): 1,
                             ("rogue.py", "<module>"): 1,
                             ("rogue.py", "quoted"): 2,
                             ("rogue.py", "commented"): 2,
                             ("rogue.py", "clause_words"): 2,
                             ("rogue.py", "dynamic"): 1})
    assert len(unpreparable) == 1 and unpreparable[0].startswith("rogue.py:24:"), unpreparable
    assert triggers == set()



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


#: The exact terminal -> non-terminal moves today's writers make, written out
#: here independently of the table. These are legacy exceptions kept so slice
#: a changes no behaviour, not successor events: slice a does not establish
#: "a terminal row moves on only through a successor". Growing this set needs
#: a deliberate edit here and in the table.
EXPECTED_LEGACY_REOPENS = frozenset(
    {("premerge_gate_park", t, "needs_human")
     for t in ("completed", "failed", "cancelled", "timed_out")}
    | {("quarantine_legacy_row", t, "needs_human")
       for t in ("completed", "failed", "cancelled", "timed_out")}
    # submit_result: a worker-reported failure revised by a later result, and
    # an implement timeout whose cancelled fallback was adopted.
    | {("result", frm, to) for frm in ("failed", "timed_out")
       for to in ("needs_human", "blocked")})


def test_legacy_reopens_are_exactly_the_documented_exceptions():
    table = {(t.event, frm, to) for t in ts.TABLE.values() for frm, to in t.legacy_reopens}
    assert table == EXPECTED_LEGACY_REOPENS
    for t in ts.TABLE.values():
        assert all(ts.is_reopen(f, to) for f, to in t.legacy_reopens), t.event
        # Every pair a row can express that would reopen and is not listed is refused.
        for frm in t.from_statuses:
            for to in t.to_statuses:
                if ts.is_reopen(frm, to):
                    assert t.allows(frm, to) == ((t.event, frm, to) in EXPECTED_LEGACY_REOPENS)


def test_next_statuses_agrees_with_allows_for_every_pair():
    for event in sorted(ts.TABLE):
        spec = ts.TABLE[event]
        for frm in sorted(ts.STATUSES):
            assert ts.next_statuses(frm, event) == frozenset(
                to for to in ts.STATUSES if spec.allows(frm, to)), (event, frm)
    assert ts.next_statuses("no_such_event", "pending") == frozenset()


def test_next_statuses_does_not_advertise_a_refused_reopen():
    assert not {"needs_human", "blocked"} & ts.next_statuses("completed", "result")
    assert {"needs_human", "blocked"} <= ts.next_statuses("failed", "result")


@pytest.mark.parametrize("prior,to", [
    ("cancelled", "needs_human"), ("cancelled", "blocked"),
    ("completed", "needs_human"), ("completed", "blocked"),
])
def test_result_cannot_reopen_outside_the_legacy_exceptions(prior, to):
    conn = _mem_db()
    conn.execute("INSERT INTO tasks (task_id, status) VALUES ('t', ?)", (prior,))
    out = ts.transition(conn, "t", "result", (prior,), to_status=to)
    assert out.refusal == ts.ILLEGAL_TRANSITION and out.reason == "reopen_not_allowed"
    assert conn.execute("SELECT status FROM tasks").fetchone()[0] == prior


def test_result_legacy_exception_still_reopens_a_failed_row():
    conn = _mem_db()
    conn.execute("INSERT INTO tasks (task_id, status) VALUES ('t', 'failed')")
    assert ts.transition(conn, "t", "result", ("failed",), to_status="needs_human").ok


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
    """Two separate checks. (1) transition() agrees with the table — that is
    consistency, and by construction uses the table as its oracle. (2) The
    invariant under test does *not* use the table: every successful
    terminal -> non-terminal move must be one of EXPECTED_LEGACY_REOPENS."""
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
                    assert (event, status, to) in EXPECTED_LEGACY_REOPENS, (run, status, event, to)
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
