"""#1081 slice a: one transition table, one status writer.

The guard compiles every SQL write literal under ``src/`` (except
``task_state.py``) against the repo schema, lets SQLite's authorizer say which
ones write ``tasks.status``, and compares it with an explicit allowlist of today's
writers (file + function, with a count, never line numbers). A new writer
fails this test; a writer moved into :func:`task_state.transition` must be
removed from the allowlist in the same PR. Later slices shrink it to empty.

Non-literal SQL is checked where it runs: every ``.execute`` /
``.executemany`` / ``.executescript`` call whose SQL argument is not a string
literal (or a name assigned only from literals in its function or module)
fails with file:line unless its (file, function) is in ``DYNAMIC_ALLOWLIST``,
whatever that SQL might contain. Any other reference to one of those methods
(an alias, a stored or passed bound method, functools.partial) fails the same
way: the guard cannot see what SQL such a value runs.
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

#: SQL execution calls (.execute/.executemany/.executescript) whose SQL
#: argument does not resolve statically to a literal: (file, function) ->
#: (sites, why). The guard cannot see what such SQL writes, so each one is
#: listed here by hand, whatever it contains; later slices route or remove them.
DYNAMIC_ALLOWLIST = {
    ("cea/store.py", "ensure_schema"): (
        4, "receipt DDL: column list joined from _COLUMN_TYPES; loops over module DDL tuples"),
    ("cea/store.py", "record_receipt"): (
        1, "INSERT INTO authorization_receipts, columns from RECEIPT_COLUMNS"),
    ("memory_hybrid.py", "_candidates"): (2, "SELECT on adr001_memory with _scope_sql clause"),
    ("memory_hybrid.py", "_lazy_embed"): (1, "SELECT on adr001_memory with IN (?, ...) placeholders"),
    ("memory_hybrid.py", "_rank_middle"): (2, "SELECTs on adr001_vec/adr001_fts with IN (?, ...)"),
    ("memory_hybrid.py", "_retrieve_ranked_once"): (1, "SELECT on adr001_memory, IN (?, ...) + scope"),
    ("memory_hybrid.py", "effective_rows"): (1, "SELECT on adr001_memory, IN (?, ...) + scope"),
    ("memory_hybrid.py", "ensure_index_schema"): (
        3, "adr001_fts rebuild and trigger DDL built with _body_sql"),
    ("memory_hybrid.py", "render_head"): (2, "SELECTs on adr001_memory with scope prefix"),
    ("memory_runtime.py", "has_active_task_decision"): (
        1, "SELECT on adr001_memory, clauses by present columns"),
    ("memory_runtime.py", "retrieve"): (1, "SELECT on adr001_memory, clauses by present columns"),
    ("memory_runtime.py", "retrieve_layer_ranked"): (
        1, "read-only SELECT on adr001_memory, layer/scope clauses and optional FTS ranking"),
    ("memory_runtime.py", "retrieve_shadow"): (1, "one SELECT of composed adr001_memory subqueries"),
    ("queue.py", "execute"): (2, "timing Connection.execute wrapper: forwards its caller's SQL"),
    ("queue.py", "_initialize_schema"): (7, "loops over module DDL/migration tuples and _DDL_INDEXES"),
    ("queue.py", "_record_end_on"): (
        1, "lease end: optional ', result_posted_at = ?' literal, no status column"),
    ("queue.py", "_store_task_telemetry"): (1, "UPDATE task_attribution, SET list from telemetry"),
    ("queue.py", "_terminalize_work_intent_on"): (1, "SELECT on tasks with IN (?, ...)"),
    ("queue.py", "_test_selection_identities_on"): (1, "SELECT on task_attribution with IN (?, ...)"),
    ("queue.py", "count_pool_grant_uses"): (1, "SELECT from class attribute _POOL_GRANT_USES_SQL"),
    ("queue.py", "record_pool_grant_use"): (1, "SELECT from class attribute _POOL_GRANT_USES_SQL"),
    ("queue.py", "dequeue"): (5, "SELECT * FROM tasks claim queries with f-string filters"),
    ("queue.py", "get_exec_state"): (1, "SELECT on tasks, columns from EXEC_STATE_COLUMNS"),
    ("queue.py", "reroute_stale_provider_bindings"): (1, "SELECT on tasks with held-type NOT IN"),
    ("queue.py", "standing_request_changes_review"): (2, "SELECTs on tasks, ORDER BY _VERDICT_AT_SQL"),
    ("watch.py", "_set"): (1, "UPDATE issue_claims, assignment from watch.py callers"),
}

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


#: printf-style and str.format fields: what ``%`` / ``.format`` fill in.
_PRINTF_FIELD = re.compile(r"%(?:\([^)]*\))?[#0 +-]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[hlL]?[diouxXeEfFgGcrsa]")
_FORMAT_FIELD = re.compile(r"\{[^{}]*\}")


def _fold(e: ast.AST):
    """The string an expression builds, with a hole per non-literal part, or
    None if it is not a string expression at all."""
    if isinstance(e, ast.Constant) and isinstance(e.value, str):
        return e.value
    if isinstance(e, ast.BinOp) and isinstance(e.op, ast.Mod):
        left = _fold(e.left)
        if left is None:
            return None
        return "%".join(_PRINTF_FIELD.sub(_HOLE, part) for part in left.split("%%"))
    if (isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute)
            and e.func.attr == "format"):
        base = _fold(e.func.value)
        if base is None:
            return None
        return "\x02".join(_FORMAT_FIELD.sub(_HOLE, part).replace("}}", "}")
                            for part in base.split("{{")).replace("\x02", "{")
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


# ── the call-site rule: SQL reaching sqlite must be literal ─────────────
#
# Classifying statements by their text missed SQL whose leading fragment is
# itself non-literal (f"{verb} tasks SET ..."), and str.join is not folded at
# all. So the dynamic rule is anchored where SQL runs: every
# .execute/.executemany/.executescript call. Its SQL argument must resolve
# statically to a literal, which the authorizer check above then compiles.
# Anything else fails with file:line unless its (file, function) is in
# DYNAMIC_ALLOWLIST, whatever that SQL might contain.

_EXEC_METHODS = frozenset({"execute", "executemany", "executescript"})
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _literal(e) -> bool:
    """A string expression with no non-literal part ('a' 'b', 'a' + 'b')."""
    text = _fold(e)
    return text is not None and _HOLE not in text


def _bindings(scope: ast.AST, name: str) -> list:
    """Every value ``scope`` binds to ``name`` itself (nested scopes excluded):
    the assigned expression, or None for any binding that is not a plain
    single-target assignment (parameter, loop, unpacking, +=, import...)."""
    out = []
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        a = scope.args
        params = a.posonlyargs + a.args + a.kwonlyargs + [v for v in (a.vararg, a.kwarg) if v]
        if name in {x.arg for x in params}:
            out.append(None)
    body = scope.body if isinstance(scope.body, list) else [scope.body]
    stack = list(body)
    while stack:
        n = stack.pop()
        if isinstance(n, _SCOPES):
            if getattr(n, "name", None) == name:
                out.append(None)
            stack.extend(n.decorator_list if not isinstance(n, ast.Lambda) else [])
            continue
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                if isinstance(t, ast.Name):
                    if t.id == name:
                        out.append(n.value)
                elif any(isinstance(x, ast.Name) and x.id == name for x in ast.walk(t)):
                    out.append(None)
            if n.value is not None:
                stack.append(n.value)
            continue
        if isinstance(n, ast.Name) and n.id == name and not isinstance(n.ctx, ast.Load):
            out.append(None)
        elif isinstance(n, (ast.Import, ast.ImportFrom)) and any(
                (al.asname or al.name.split(".")[0]) == name for al in n.names):
            out.append(None)
        elif isinstance(n, (ast.Global, ast.Nonlocal)) and name in n.names:
            out.append(None)
        elif isinstance(n, ast.ExceptHandler) and n.name == name:
            out.append(None)
        stack.extend(ast.iter_child_nodes(n))
    return out


def _sql_resolves(arg, chain: list) -> bool:
    """Whether a call's SQL argument is a literal, or a Name that the nearest
    scope binding it (innermost function outwards, then the module) assigns
    only from literals."""
    if _literal(arg):
        return True
    if not isinstance(arg, ast.Name):
        return False
    for scope in chain:
        values = _bindings(scope, arg.id)
        if values:
            return all(v is not None and _literal(v) for v in values)
    return False


def _unresolved_exec_calls(tree: ast.Module, rel: str) -> dict:
    """(file, function) -> ``file:line`` of each SQL execution call whose SQL
    argument does not resolve to a literal."""
    out: dict = {}
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}

    def visit(node, chain, fn_name):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                visit(child, [child] + chain, getattr(child, "name", fn_name))
                continue
            if (isinstance(child, ast.Attribute) and child.attr in _EXEC_METHODS
                    and id(child) not in called):
                # Fail closed: an execute method used as a value (aliased,
                # stored, passed, partial'd) runs SQL this rule cannot see.
                out.setdefault((rel, fn_name), []).append(f"{rel}:{child.lineno}")
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                    and child.func.attr in _EXEC_METHODS):
                arg = child.args[0] if child.args else None
                if arg is None or isinstance(arg, ast.Starred) or not _sql_resolves(arg, chain):
                    out.setdefault((rel, fn_name), []).append(f"{rel}:{child.lineno}")
            visit(child, chain, fn_name)

    visit(tree, [tree], "<module>")
    return out


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
    """(writers, unpreparable, triggers, dynamic) for every .py under ``root``.
    ``dynamic`` maps (file, function) to the ``file:line`` of each SQL
    execution call whose SQL argument is not statically literal."""
    found: Counter = Counter()
    unpreparable: list = []
    triggers: set = set()
    dynamic: dict = {}
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
        for where, locs in _unresolved_exec_calls(tree, rel).items():
            dynamic.setdefault(where, []).extend(locs)
        for node, text in _string_exprs(tree):
            for st in _statements(text):
                if not _WRITE_HEAD.match(_head(st)):
                    continue
                where = (rel, owner.get(id(node), "<module>"))
                try:
                    own, fired = _status_writes(conn, st)
                except Unpreparable as e:
                    if _HOLE in st:  # its call site answers to DYNAMIC_ALLOWLIST
                        continue
                    unpreparable.append(f"{rel}:{node.lineno}: {e}: {st.strip()[:120]!r}")
                    continue
                if own:
                    found[where] += 1
                triggers |= fired
    return found, unpreparable, triggers, dynamic


def _unallowed_dynamic(dynamic: dict, allowlist: dict = DYNAMIC_ALLOWLIST) -> list:
    """``file:line`` of every non-literal SQL call not covered by ``allowlist``
    (a function over its listed count reports all its sites)."""
    return sorted(loc for where, locs in dynamic.items()
                  if len(locs) > allowlist.get(where, (0, ""))[0] for loc in locs)


def test_no_status_writer_outside_the_allowlist(schema):
    found, unpreparable, triggers, dynamic = _scan(schema)
    bad = _unallowed_dynamic(dynamic)
    assert not bad, (
        "non-literal SQL at an execute call — the guard cannot see what it writes, "
        f"so it must be literal or listed in DYNAMIC_ALLOWLIST with a reason: {bad}")
    stale = {w for w in DYNAMIC_ALLOWLIST if w not in dynamic}
    assert not stale, f"DYNAMIC_ALLOWLIST entries {sorted(stale)} no longer exist: remove them"
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
        "    conn.execute('UPDATE tasks SET no_such_column=? WHERE task_id=?')\n"
        "def dyn_column(conn, column, value, tid):\n"
        "    conn.execute('UPDATE tasks SET ' + column + '=? WHERE task_id=?', (value, tid))\n"
        "def dyn_fstring(conn, column):\n"
        "    conn.execute(f'UPDATE tasks SET {column}=? WHERE task_id=?')\n"
        "def dyn_percent(conn, column):\n"
        "    conn.execute('UPDATE tasks SET %s=? WHERE task_id=?' % column)\n"
        "def dyn_format(conn, column):\n"
        "    conn.execute('UPDATE tasks SET {}=? WHERE task_id=?'.format(column))\n"
        "def dyn_target(conn, table):\n"
        "    conn.execute('UPDATE ' + table + ' SET summary=?')\n"
        "def dyn_other_table(conn, column):\n"
        "    conn.execute(f'UPDATE gates SET {column}=? WHERE rowid=?')\n")
    found, unpreparable, triggers, dynamic = _scan(schema, tmp_path)
    assert found == Counter({("rogue.py", "sneak"): 1,
                             ("rogue.py", "<module>"): 1,
                             ("rogue.py", "quoted"): 2,
                             ("rogue.py", "commented"): 2,
                             ("rogue.py", "clause_words"): 2,
                             ("rogue.py", "dynamic"): 1})
    assert len(unpreparable) == 1 and unpreparable[0].startswith("rogue.py:24:"), unpreparable
    assert triggers == set()
    # Round 4: a non-literal tasks write is never certified by a filler that
    # happens to compile; it needs its (file, function) allowlisted.
    assert dynamic == {("rogue.py", "quoted"): ["rogue.py:13"],  # f-string, even a constant field
                       ("rogue.py", "dynamic"): ["rogue.py:22"],
                       ("rogue.py", "dyn_column"): ["rogue.py:26"],
                       ("rogue.py", "dyn_fstring"): ["rogue.py:28"],
                       ("rogue.py", "dyn_percent"): ["rogue.py:30"],
                       ("rogue.py", "dyn_format"): ["rogue.py:32"],
                       ("rogue.py", "dyn_target"): ["rogue.py:34"],
                       # Round 5: the call-site rule does not look at what the
                       # SQL writes, so a non-tasks dynamic write needs listing too.
                       ("rogue.py", "dyn_other_table"): ["rogue.py:36"]}, dynamic
    assert _unallowed_dynamic(dynamic) == sorted(
        loc for locs in dynamic.values() for loc in locs)
    allow = {("rogue.py", f): (1, "test") for f in
             ("quoted", "dynamic", "dyn_column", "dyn_fstring", "dyn_percent", "dyn_format",
              "dyn_other_table")}
    assert _unallowed_dynamic(dynamic, allow) == ["rogue.py:34"]
    allow[("rogue.py", "dyn_target")] = (1, "test")
    assert _unallowed_dynamic(dynamic, allow) == []


def test_dynamic_column_without_literal_status_is_not_certified(schema, tmp_path):
    """The round-4 bypass: the first filler (rowid) compiles and writes no
    status, but ``column='status'`` does at runtime."""
    pkg = tmp_path / "agent_crew"
    pkg.mkdir()
    (pkg / "m.py").write_text(
        "def set_col(conn, column, value, tid):\n"
        "    conn.execute('UPDATE tasks SET ' + column + '=? WHERE task_id=?', (value, tid))\n")
    found, unpreparable, _, dynamic = _scan(schema, tmp_path)
    assert found == Counter() and unpreparable == []  # what the old guard concluded
    assert _unallowed_dynamic(dynamic) == ["m.py:2"]
    assert _unallowed_dynamic(dynamic, {("m.py", "set_col"): (1, "test")}) == []
    assert _unallowed_dynamic(dynamic, {("m.py", "other"): (1, "test")}) == ["m.py:2"]


@pytest.mark.parametrize("src,text", [
    ("'SET %s=? WHERE x=%(k)s' % a", "SET \x01=? WHERE x=\x01"),
    ("'100%% %d' % n", "100% \x01"),
    ("'SET {}=? {name}'.format(a, name=b)", "SET \x01=? \x01"),
    ("'{{lit}} {0}'.format(a)", "{lit} \x01"),
    ("f'a{b}c' + d", "a\x01c\x01"),
])
def test_fold_marks_every_non_literal_fragment(src, text):
    assert _fold(ast.parse(src, mode="eval").body) == text



# Round 5: the dynamic rule lives at the execute call, not in the SQL's text.
_CALLSITE_MODULE = (
    "MOD_SQL = 'UPDATE tasks SET summary=? WHERE task_id=?'\n"
    "def leading_hole(conn, verb, status, tid):\n"
    "    conn.execute(f'{verb} tasks SET status=? WHERE task_id=?', (status, tid))\n"
    "def joined(conn):\n"
    "    conn.execute(' '.join(['UPDATE', 'tasks', 'SET status=?']), ('x',))\n"
    "def passed_in(conn, sql):\n"
    "    conn.execute(sql, ('x',))\n"
    "def from_call(conn):\n"
    "    conn.executemany(build_sql(), [])\n"
    "def rebound(conn, cond):\n"
    "    sql = 'UPDATE tasks SET summary=?'\n"
    "    if cond:\n"
    "        sql = sql + ', status=?'\n"
    "    conn.execute(sql)\n"
    "def looped(conn, stmts):\n"
    "    for sql in stmts:\n"
    "        conn.executescript(sql)\n"
    "def no_args(conn):\n"
    "    conn.execute(*args)\n"
    "def literal_local(conn):\n"
    "    sql = 'UPDATE tasks SET summary=? WHERE task_id=?'\n"
    "    conn.execute(sql, ('x', 't'))\n"
    "def literal_module(conn):\n"
    "    conn.execute(MOD_SQL, ('x', 't'))\n"
    "def literal_concat(conn):\n"
    "    conn.execute('UPDATE tasks SET summary=? ' + 'WHERE task_id=?', ('x', 't'))\n"
    "def shadowed(conn, MOD_SQL):\n"
    "    conn.execute(MOD_SQL)\n")


def _callsite_scan(schema, tmp_path):
    pkg = tmp_path / "agent_crew"
    pkg.mkdir()
    (pkg / "c.py").write_text(_CALLSITE_MODULE)
    return _scan(schema, tmp_path)


def test_non_literal_sql_fails_at_its_execute_call(schema, tmp_path):
    found, unpreparable, _, dynamic = _callsite_scan(schema, tmp_path)
    assert dynamic == {("c.py", "leading_hole"): ["c.py:3"],  # the round-5 bypass
                       ("c.py", "joined"): ["c.py:5"],
                       ("c.py", "passed_in"): ["c.py:7"],
                       ("c.py", "from_call"): ["c.py:9"],
                       ("c.py", "rebound"): ["c.py:14"],
                       ("c.py", "looped"): ["c.py:17"],
                       ("c.py", "no_args"): ["c.py:19"],
                       ("c.py", "shadowed"): ["c.py:28"]}, dynamic
    # Literal SQL, directly or through a literal-only name, still goes through
    # the authorizer and writes no status. The text scan cannot see `joined`
    # (no fragment is a statement) or `rebound`'s ', status=?' (it follows a
    # hole); only their call sites catch them.
    assert found == Counter() and unpreparable == []
    assert _unallowed_dynamic(dynamic) == sorted(l for ls in dynamic.values() for l in ls)


def test_allowlisted_function_may_run_non_literal_sql(schema, tmp_path):
    _, _, _, dynamic = _callsite_scan(schema, tmp_path)
    allow = {w: (len(locs), "test") for w, locs in dynamic.items()}
    assert _unallowed_dynamic(dynamic, allow) == []
    del allow[("c.py", "leading_hole")]
    assert _unallowed_dynamic(dynamic, allow) == ["c.py:3"]
    # A listed count is a cap: a second site in the same function fails both.
    assert _unallowed_dynamic({("c.py", "f"): ["c.py:1", "c.py:2"]},
                              {("c.py", "f"): (1, "test")}) == ["c.py:1", "c.py:2"]


# Round 6: an execute method used as a value fails closed, whatever SQL it
# later runs (scope decision on #1081: one bounded rule, no more parsing).
_ALIAS_MODULE = (
    "import functools\n"
    "def aliased(conn):\n"
    "    run = conn.execute\n"
    "    run(' '.join(['UPDATE', 'tasks', 'SET status=?']), ('x',))\n"
    "def passed(conn):\n"
    "    helper(conn.executemany)\n"
    "def partial(conn):\n"
    "    functools.partial(conn.executescript)('UPDATE tasks SET summary=1')\n"
    "def stored(self, conn):\n"
    "    self.run = conn.execute\n"
    "def direct(conn):\n"
    "    conn.execute('UPDATE tasks SET summary=? WHERE task_id=?', ('x', 't'))\n")


def _alias_scan(schema, tmp_path):
    pkg = tmp_path / "agent_crew"
    pkg.mkdir()
    (pkg / "a.py").write_text(_ALIAS_MODULE)
    return _scan(schema, tmp_path)


def test_execute_method_used_as_a_value_fails_closed(schema, tmp_path):
    found, _, _, dynamic = _alias_scan(schema, tmp_path)
    assert dynamic == {("a.py", "aliased"): ["a.py:3"],  # the round-6 bypass
                       ("a.py", "passed"): ["a.py:6"],
                       ("a.py", "partial"): ["a.py:8"],
                       ("a.py", "stored"): ["a.py:10"]}, dynamic
    assert found == Counter()
    assert _unallowed_dynamic(dynamic) == ["a.py:10", "a.py:3", "a.py:6", "a.py:8"]


def test_execute_alias_in_an_allowlisted_function_passes(schema, tmp_path):
    _, _, _, dynamic = _alias_scan(schema, tmp_path)
    allow = {w: (len(locs), "test") for w, locs in dynamic.items()}
    assert _unallowed_dynamic(dynamic, allow) == []
    del allow[("a.py", "aliased")]
    assert _unallowed_dynamic(dynamic, allow) == ["a.py:3"]


def test_every_dynamic_allowlist_entry_has_a_reason_and_exact_count(schema):
    _, _, _, dynamic = _scan(schema)
    assert {w: len(locs) for w, locs in dynamic.items()} == {
        w: n for w, (n, _) in DYNAMIC_ALLOWLIST.items()}
    assert all(why.strip() for _, why in DYNAMIC_ALLOWLIST.values())
    assert sum(n for n, _ in DYNAMIC_ALLOWLIST.values()) == 46



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
