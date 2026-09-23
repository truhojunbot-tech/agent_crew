"""§3 receipt store — ``authorization_receipts`` + ``dispatch_nonces`` (ADR §3, P2, P4).

Properties this module owns, all of them stated in the contract:

* **The frozen schema is the column set.** Every top-level key of
  ``tests/cea_contract/receipt.schema.json`` is a column; nested objects and
  arrays are stored as JSON text. Nothing is written that does not validate
  (:func:`agent_crew.cea.schema.validate_receipt`).
* **Append-only.** ``UPDATE`` and ``DELETE`` on ``authorization_receipts`` raise
  (§3 last line; the property Codex #6 found missing on ``task_exec_events``).
  A lifecycle change is therefore a *new row* with the same ``receipt_id`` and
  the next ``seq``; the receipt's current state is the row with the highest
  ``seq``.
* **Nonces are single-use.** ``dispatch_nonces`` is a claim table, not a log:
  consuming a nonce is one conditional ``UPDATE`` (``used_at IS NULL``), so two
  racing consumers cannot both win (P2 dispatch/execute-start rows).

Step 2c added the ``tasks.receipt_id`` requirement (``_DDL_TASKS_RECEIPT_REQUIRED``);
the sole-writer property and the five ``validate_*`` call sites live in
:mod:`agent_crew.queue` and :mod:`agent_crew.cea.callsites`. Still not here: the
foreign key onto ``authorization_receipts`` (the receipt table is append-only, so
the FK target is not a unique column), and the §7 ingress adapters (step 2b).
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Callable, Optional

from agent_crew.cea.schema import canonical_json, schema_required_fields, validate_receipt

# Columns that hold a nested object/array; stored as JSON text.
_JSON_COLUMNS = frozenset({
    "authority_source", "source_decision_revs", "capability_registry", "matched_capability",
    "reuse", "provider_budget", "human_gate_state", "reason", "signature", "binding",
    "dispatch_nonces", "supersedes", "provenance",
})

_COLUMN_TYPES: dict[str, str] = {
    "receipt_id": "TEXT NOT NULL",
    "issued_at": "TEXT NOT NULL",
    "issuer": "TEXT NOT NULL",
    "task_id": "TEXT NOT NULL",
    "intent_hash": "TEXT NOT NULL",
    "parent_receipt_id": "TEXT",
    "project": "TEXT NOT NULL",
    "authority_source": "TEXT NOT NULL",
    "policy_generation": "INTEGER NOT NULL",
    "policy_hash": "TEXT NOT NULL",
    "source_decision_revs": "TEXT NOT NULL",
    "capability_registry": "TEXT NOT NULL",
    "matched_capability": "TEXT",
    "reuse": "TEXT",
    "runtime_state": "TEXT NOT NULL",
    "provider_budget": "TEXT NOT NULL",
    "required_reviewer": "TEXT",
    "required_tester": "TEXT",
    "human_gate_state": "TEXT NOT NULL",
    "caller_identity": "TEXT",
    "caller_provenance": "TEXT NOT NULL",
    "executor_binding": "TEXT",
    "executor_binding_status": "TEXT NOT NULL",
    "caller_identity_status": "TEXT NOT NULL",
    "downgrade_reason": "TEXT",
    "decision": "TEXT NOT NULL",
    "reason": "TEXT NOT NULL",
    "signature": "TEXT NOT NULL",
    "state": "TEXT NOT NULL",
    "binding": "TEXT NOT NULL",
    "idempotency_key": "TEXT NOT NULL",
    "attempt": "INTEGER NOT NULL",
    "max_attempts": "INTEGER NOT NULL",
    "dispatch_nonces": "TEXT NOT NULL",
    "supersedes": "TEXT NOT NULL",
    "provenance": "TEXT",
}

RECEIPT_COLUMNS: tuple[str, ...] = tuple(_COLUMN_TYPES)

_DDL_AUTHORIZATION_RECEIPTS = (
    "CREATE TABLE IF NOT EXISTS authorization_receipts (\n"
    "    row_id      INTEGER PRIMARY KEY AUTOINCREMENT,\n"
    "    seq         INTEGER NOT NULL,\n"
    + "".join(f"    {name:<24}{decl},\n" for name, decl in _COLUMN_TYPES.items())
    + "    receipt_json TEXT NOT NULL,\n"
    "    recorded_at REAL NOT NULL,\n"
    "    recorded_by TEXT,\n"
    "    note        TEXT,\n"
    "    UNIQUE (receipt_id, seq)\n"
    ")"
)

_DDL_AUTHORIZATION_RECEIPTS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_receipts_receipt ON authorization_receipts(receipt_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_receipts_task ON authorization_receipts(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_receipts_intent ON authorization_receipts(intent_hash)",
)

# §3: "Receipts are append-only (authorization_receipts with no UPDATE/DELETE,
# enforced by trigger)". The triggers are the enforcement; application code that
# forgets the rule fails loudly instead of silently rewriting an audit record.
_DDL_AUTHORIZATION_RECEIPTS_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS trg_authorization_receipts_no_update\n"
    "BEFORE UPDATE ON authorization_receipts BEGIN\n"
    "    SELECT RAISE(ABORT, 'authorization_receipts is append-only (ADR §3): "
    "record a new seq instead of updating');\n"
    "END",
    "CREATE TRIGGER IF NOT EXISTS trg_authorization_receipts_no_delete\n"
    "BEFORE DELETE ON authorization_receipts BEGIN\n"
    "    SELECT RAISE(ABORT, 'authorization_receipts is append-only (ADR §3): "
    "receipts are never deleted');\n"
    "END",
)

# P2: "a fresh dispatch nonce is minted and bound to (receipt_id, attempt) ... a
# nonce is single-use" (P4). Consuming is an atomic conditional UPDATE, never
# check-then-act.
_DDL_DISPATCH_NONCES = """
CREATE TABLE IF NOT EXISTS dispatch_nonces (
    nonce       TEXT PRIMARY KEY,
    receipt_id  TEXT NOT NULL,
    attempt     INTEGER NOT NULL,
    issued_at   TEXT NOT NULL,
    used_at     TEXT,
    used_by     TEXT,
    UNIQUE (receipt_id, attempt)
)
"""

# P4: "a unique partial index on ``intent_hash`` for live lineages". The receipt
# table is append-only, so one live lineage legitimately has many rows
# (ISSUED → QUEUED → CLAIMED → …) and a partial index over it cannot express
# "at most one". The constraint therefore lives in its own claim table, where
# ``intent_hash`` is the PRIMARY KEY: a second admission of the same intent loses
# the INSERT rather than losing a race. ⛔Not check-then-act — two adapters both
# reading "no live lineage" and both inserting is exactly the duplicate E10 4c
# found, and only the database can arbitrate it.
#
# A CONSUMED lineage keeps its row: that is what makes ALREADY_COMPLETED
# answerable (P4). SUPERSEDED/REVOKED release it, because re-admission is the
# intended outcome there.
_DDL_INTENT_LINEAGES = """
CREATE TABLE IF NOT EXISTS intent_lineages (
    intent_hash TEXT PRIMARY KEY,
    receipt_id  TEXT NOT NULL,
    project     TEXT NOT NULL,
    state       TEXT NOT NULL,
    claimed_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
)
"""

_DDL_MIGRATE_LINEAGE_WORK_HASH = "ALTER TABLE intent_lineages ADD COLUMN work_hash TEXT"
"""P4's completed-work question is asked of the *work*, not of the exact intent.

``intent_hash`` includes ``authority_decision_ids``, so adding an id to a request
changes the hash and the completed lineage is simply not found. ``work_hash`` is
the same identity with the authority ids removed, which is what "this work
already completed" actually ranges over. Nullable and additive: rows written
before this column exists read back as ``NULL`` and are only invisible to the
supersession check, never mis-answered by it."""

_DDL_LINEAGE_WORK_HASH_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_intent_lineages_work_hash "
    "ON intent_lineages (work_hash, state)")

_DDL_MIGRATE_TASKS_RECEIPT_ID = "ALTER TABLE tasks ADD COLUMN receipt_id TEXT"
"""Step 1 added the column nullable. Step 2c makes it required — see below."""

_DDL_TASKS_RECEIPT_REQUIRED = (
    "CREATE TRIGGER IF NOT EXISTS trg_tasks_receipt_id_required\n"
    "BEFORE INSERT ON tasks\n"
    "WHEN NEW.receipt_id IS NULL OR TRIM(NEW.receipt_id) = '' BEGIN\n"
    "    SELECT RAISE(ABORT, 'tasks.receipt_id is required (ADR P2): a task row is the "
    "physical form of an admission decision, so it may not exist without the receipt that "
    "made it');\n"
    "END",
    "CREATE TRIGGER IF NOT EXISTS trg_tasks_receipt_id_immutable\n"
    "BEFORE UPDATE OF receipt_id ON tasks\n"
    "WHEN NEW.receipt_id IS NULL OR TRIM(NEW.receipt_id) = ''\n"
    "  OR (OLD.receipt_id IS NOT NULL AND NEW.receipt_id <> OLD.receipt_id) BEGIN\n"
    "    SELECT RAISE(ABORT, 'tasks.receipt_id is fixed at admission (ADR P2): clearing or "
    "repointing it would detach a live row from the decision that admitted it');\n"
    "END",
)
"""``tasks.receipt_id NOT NULL``, expressed the only way SQLite allows in place.

``ALTER TABLE ... SET NOT NULL`` does not exist in SQLite, and the alternative —
rebuild ``tasks`` into a new table and swap — rewrites the live queue of a
running server to add a constraint. A ``BEFORE INSERT`` trigger enforces the same
property at the same place (the database, not the application) and is additive
and idempotent on a live DB.

⛔It is deliberately an INSERT trigger, not a backfill. Rows written before this
  step have no receipt and there is no honest value to give them: inventing one
  would put a receipt_id in the audit trail that names a decision nobody made.
  They keep ``NULL``, the property holds from here forward, and the second
  trigger stops a later ``UPDATE`` from clearing or repointing a row that has one.
"""

LIVE_STATES = ("ISSUED", "QUEUED", "CLAIMED", "RUNNING", "HELD")
TERMINAL_STATES = ("CONSUMED", "SUPERSEDED", "REVOKED")

LIFECYCLE_GRAPH: dict[str, frozenset[str]] = {
    # §3 lifecycle: ISSUED → QUEUED → CLAIMED → RUNNING → CONSUMED, with HELD as
    # the deferral the validator returns to the engine, and SUPERSEDED/REVOKED
    # reachable from any live state (re-admission, revocation).
    "ISSUED":     frozenset({"ISSUED", "QUEUED", "HELD", "SUPERSEDED", "REVOKED"}),
    "QUEUED":     frozenset({"QUEUED", "CLAIMED", "HELD", "SUPERSEDED", "REVOKED"}),
    # CLAIMED → CONSUMED is legal: the validator's RESULT point accepts a receipt
    # still in CLAIMED, because a one-shot agent can finish without ever posting
    # /start. Requiring RUNNING first would refuse real results.
    "CLAIMED":    frozenset({"CLAIMED", "RUNNING", "CONSUMED", "HELD", "SUPERSEDED", "REVOKED"}),
    "RUNNING":    frozenset({"RUNNING", "CONSUMED", "SUPERSEDED", "REVOKED"}),
    "HELD":       frozenset({"HELD", "QUEUED", "CLAIMED", "SUPERSEDED", "REVOKED"}),
    # terminal states are final — P4 replay refusal is only a fact if nothing
    # can walk a receipt back out of one.
    "CONSUMED":   frozenset(),
    "SUPERSEDED": frozenset(),
    "REVOKED":    frozenset(),
}
"""Which lifecycle transitions ``append_lifecycle`` will record. Self-transitions
are permitted because a revision may carry only a ``mutate`` (minting a dispatch
nonce on a CLAIMED receipt appends a new CLAIMED row)."""


class ReceiptStoreError(Exception):
    """A receipt was rejected before it reached the table (contract violation)."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create/migrate the receipt store. Additive and idempotent: safe to run on
    every open of an existing live DB (the migration test proves it)."""
    conn.execute(_DDL_AUTHORIZATION_RECEIPTS)
    for stmt in _DDL_AUTHORIZATION_RECEIPTS_INDEXES:
        conn.execute(stmt)
    for stmt in _DDL_AUTHORIZATION_RECEIPTS_TRIGGERS:
        conn.execute(stmt)
    conn.execute(_DDL_DISPATCH_NONCES)
    conn.execute(_DDL_INTENT_LINEAGES)
    try:
        conn.execute(_DDL_MIGRATE_LINEAGE_WORK_HASH)
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.execute(_DDL_LINEAGE_WORK_HASH_INDEX)
    try:
        conn.execute(_DDL_MIGRATE_TASKS_RECEIPT_ID)
    except sqlite3.OperationalError:
        pass  # column already exists, or `tasks` not created yet on this connection
    for stmt in _DDL_TASKS_RECEIPT_REQUIRED:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # `tasks` is not on this connection (receipt-store-only DBs)


def _encode(receipt: dict) -> dict:
    row: dict[str, Any] = {}
    for col in RECEIPT_COLUMNS:
        value = receipt.get(col)
        if col in _JSON_COLUMNS:
            row[col] = None if value is None else json.dumps(value, sort_keys=True, ensure_ascii=False)
        else:
            row[col] = value
    return row


def record_receipt(conn: sqlite3.Connection, receipt: dict, *,
                   recorded_by: Optional[str] = None, note: Optional[str] = None,
                   seq: Optional[int] = None) -> int:
    """Append one receipt row. Returns the ``seq`` written.

    The receipt is validated against the frozen schema first: an invalid receipt
    never reaches the table, so "the row exists" and "the contract held" are the
    same fact. BLOCK/HUMAN_GATE receipts are stored too — P2 requires the audit
    row even when no task row is written.
    """
    errors = validate_receipt(receipt)
    if errors:
        raise ReceiptStoreError(f"receipt violates the frozen contract: {errors[0]} ({len(errors)} error(s))")
    row = _encode(receipt)
    if seq is None:
        cur = conn.execute("SELECT COALESCE(MAX(seq), -1) + 1 FROM authorization_receipts WHERE receipt_id = ?",
                           (receipt["receipt_id"],)).fetchone()
        seq = int(cur[0])
    cols = ["seq"] + list(RECEIPT_COLUMNS) + ["receipt_json", "recorded_at", "recorded_by", "note"]
    values = [seq] + [row[c] for c in RECEIPT_COLUMNS] + [canonical_json(receipt), time.time(), recorded_by, note]
    conn.execute(
        f"INSERT INTO authorization_receipts ({', '.join(cols)}) VALUES ({', '.join(['?'] * len(cols))})",
        values)
    return seq


def append_lifecycle(conn: sqlite3.Connection, receipt_id: str, state: str, *,
                     recorded_by: Optional[str] = None, note: Optional[str] = None,
                     mutate: Optional[dict] = None,
                     sign: Optional[Callable[[dict], dict]] = None) -> dict:
    """Record a lifecycle transition as a **new row** (append-only, §3).

    Reads the current receipt, applies ``state`` (plus any ``mutate`` fields the
    transition legitimately changes, e.g. ``dispatch_nonces`` after a mint), and
    appends it at the next ``seq``. Returns the new receipt body.

    The transition must be one :data:`LIFECYCLE_GRAPH` names, checked against the
    current head **inside the caller's transaction**, so a concurrent writer
    cannot interleave a second successor onto the same head. Terminal states have
    no successors: without this, ``ISSUED → CONSUMED → RUNNING`` was accepted and
    ``current_receipt`` then answered RUNNING, which defeats the P4 replay refusal
    the append-only table exists to support (codex review of 10153bf, P1 #3).

    ``sign`` re-signs the revised body after ``state``/``mutate`` are applied and
    before it is written. A signature covers the state it was made over, so a
    caller that must re-sign has to do it *inside* this guard; passing the signing
    step is what lets the engine stop calling :func:`record_receipt` directly and
    losing the graph check with it (codex re-review of 4f79ce4, P1 #2).
    """
    state = (state or "").strip().upper()
    if state not in LIFECYCLE_GRAPH:
        raise ReceiptStoreError(
            f"unknown lifecycle state {state!r}; expected one of {tuple(LIFECYCLE_GRAPH)}")
    # Read the head and append under one write lock, so two callers cannot both
    # read the same head and both append a successor to it. When the caller is
    # already inside a transaction (the ENQUEUE path holds one), we join theirs.
    own_txn = not conn.in_transaction
    if own_txn:
        conn.execute("BEGIN IMMEDIATE")
    try:
        updated = _append_lifecycle_in_txn(conn, receipt_id, state,
                                           recorded_by=recorded_by, note=note, mutate=mutate,
                                           sign=sign)
    except BaseException:
        if own_txn:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
        raise
    if own_txn:
        conn.execute("COMMIT")
    return updated


def _append_lifecycle_in_txn(conn: sqlite3.Connection, receipt_id: str, state: str, *,
                             recorded_by: Optional[str], note: Optional[str],
                             mutate: Optional[dict],
                             sign: Optional[Callable[[dict], dict]] = None) -> dict:
    """The graph check and the append itself; the caller holds the write lock."""
    current = current_receipt(conn, receipt_id)
    if current is None:
        raise ReceiptStoreError(f"unknown receipt_id {receipt_id!r}")
    frm = str(current.get("state") or "")
    allowed = LIFECYCLE_GRAPH.get(frm)
    if allowed is None:
        raise ReceiptStoreError(
            f"receipt {receipt_id!r} is in unknown state {frm!r}; refusing to move it")
    if state not in allowed:
        if frm in TERMINAL_STATES:
            raise ReceiptStoreError(
                f"{frm} is terminal: receipt {receipt_id!r} may not become {state} (§3, P4)")
        raise ReceiptStoreError(
            f"{frm} → {state} is not a lifecycle transition for receipt {receipt_id!r} "
            f"(§3 permits {sorted(allowed)})")
    updated = dict(current)
    updated["state"] = state
    if mutate:
        updated.update(mutate)
    if sign is not None:
        updated = sign(updated)
    record_receipt(conn, updated, recorded_by=recorded_by, note=note)
    return updated


def current_receipt(conn: sqlite3.Connection, receipt_id: str) -> Optional[dict]:
    """The receipt as of its highest ``seq`` — the append-only table's "current" view."""
    row = conn.execute(
        "SELECT receipt_json FROM authorization_receipts WHERE receipt_id = ? ORDER BY seq DESC LIMIT 1",
        (receipt_id,)).fetchone()
    if row is None:
        return None
    return json.loads(row[0] if not isinstance(row, sqlite3.Row) else row["receipt_json"])


def receipt_history(conn: sqlite3.Connection, receipt_id: str) -> list[dict]:
    """Every recorded revision, oldest first (the audit trail; never truncated)."""
    rows = conn.execute(
        "SELECT seq, state, receipt_json, recorded_at, recorded_by, note "
        "FROM authorization_receipts WHERE receipt_id = ? ORDER BY seq ASC", (receipt_id,)).fetchall()
    out = []
    for r in rows:
        seq, state, body, recorded_at, recorded_by, note = (
            (r["seq"], r["state"], r["receipt_json"], r["recorded_at"], r["recorded_by"], r["note"])
            if isinstance(r, sqlite3.Row) else r)
        out.append({"seq": seq, "state": state, "receipt": json.loads(body),
                    "recorded_at": recorded_at, "recorded_by": recorded_by, "note": note})
    return out


def mint_nonce(conn: sqlite3.Connection, receipt_id: str, attempt: int, nonce: str,
               issued_at: str) -> None:
    """Bind a single-use dispatch nonce to ``(receipt_id, attempt)`` (P2 dispatch).

    The ``UNIQUE(receipt_id, attempt)`` constraint is the guarantee that one
    attempt never has two live nonces; a retry with a new attempt gets a new one.
    """
    conn.execute("INSERT INTO dispatch_nonces (nonce, receipt_id, attempt, issued_at, used_at, used_by) "
                 "VALUES (?, ?, ?, ?, NULL, NULL)", (nonce, receipt_id, attempt, issued_at))


EXECUTE_START_CONSUMER = "execute_start"
"""The only consumer P2 lets spend a dispatch nonce.

``used_by`` is written as ``"execute_start:<presenter>"`` so RESULT can ask the
claim table *who* spent the nonce, not merely whether it is spent. Without this
the nonce was a bearer proof: a worker could present it straight at ``/result``
and never ask for the go/no-go (Codex review of 4d8538d, P1).
"""


def consumer_tag(who: Optional[str], *, consumer: str = EXECUTE_START_CONSUMER) -> str:
    """``"<consumer>:<who>"`` — the value :func:`consume_nonce` stores in ``used_by``."""
    return f"{consumer}:{who or ''}"


def consumed_by_execute_start(row: Optional[dict]) -> bool:
    """Did EXECUTE_START spend this nonce row? ``False`` for unspent, unknown, or
    spent-by-something-else — the three cases RESULT must refuse alike."""
    if not row or not row.get("used_at"):
        return False
    return str(row.get("used_by") or "").startswith(EXECUTE_START_CONSUMER + ":")


def consume_nonce(conn: sqlite3.Connection, nonce: str, *, used_by: Optional[str] = None,
                  used_at: Optional[str] = None) -> bool:
    """Atomically spend a nonce. ``True`` exactly once per nonce; ``False`` for an
    unknown or already-spent nonce (P4 "a nonce is single-use")."""
    cur = conn.execute(
        "UPDATE dispatch_nonces SET used_at = ?, used_by = ? WHERE nonce = ? AND used_at IS NULL",
        (used_at or _now_rfc3339(), used_by, nonce))
    return cur.rowcount == 1


def nonce_row(conn: sqlite3.Connection, nonce: str) -> Optional[dict]:
    row = conn.execute("SELECT nonce, receipt_id, attempt, issued_at, used_at, used_by "
                       "FROM dispatch_nonces WHERE nonce = ?", (nonce,)).fetchone()
    if row is None:
        return None
    keys = ("nonce", "receipt_id", "attempt", "issued_at", "used_at", "used_by")
    return {k: (row[k] if isinstance(row, sqlite3.Row) else row[i]) for i, k in enumerate(keys)}


# ── P4 lineage claims ───────────────────────────────────────────────────────

def claim_lineage(conn: sqlite3.Connection, intent_hash: str, receipt_id: str,
                  project: str, state: str,
                  work_hash: Optional[str] = None) -> tuple[bool, Optional[str]]:
    """Atomically claim ``intent_hash`` for this receipt.

    Returns ``(True, receipt_id)`` for the winner and ``(False, holder)`` for a
    caller that lost the race, naming the receipt that holds the lineage so the
    adapter can answer ``409 DUPLICATE_INTENT`` with a usable id.
    """
    now = time.time()
    cur = conn.execute(
        "INSERT INTO intent_lineages (intent_hash, receipt_id, project, state, claimed_at, "
        "updated_at, work_hash) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(intent_hash) DO NOTHING",
        (intent_hash, receipt_id, project, state, now, now, work_hash))
    if cur.rowcount == 1:
        return True, receipt_id
    return False, _lineage_holder(conn, intent_hash)


def _lineage_holder(conn: sqlite3.Connection, intent_hash: str) -> Optional[str]:
    row = conn.execute("SELECT receipt_id FROM intent_lineages WHERE intent_hash = ?",
                       (intent_hash,)).fetchone()
    if row is None:
        return None
    return row["receipt_id"] if isinstance(row, sqlite3.Row) else row[0]


def lineage_for_intent(conn: sqlite3.Connection, intent_hash: str) -> Optional[dict]:
    """The lineage currently occupying ``intent_hash``, or ``None``."""
    row = conn.execute(
        "SELECT intent_hash, receipt_id, project, state, claimed_at, updated_at "
        "FROM intent_lineages WHERE intent_hash = ?", (intent_hash,)).fetchone()
    if row is None:
        return None
    keys = ("intent_hash", "receipt_id", "project", "state", "claimed_at", "updated_at")
    return {k: (row[k] if isinstance(row, sqlite3.Row) else row[i]) for i, k in enumerate(keys)}


def completed_lineage_for_work(conn: sqlite3.Connection, work_hash: str,
                               *, exclude_intent_hash: Optional[str] = None) -> Optional[dict]:
    """A CONSUMED lineage for the same *work*, whatever authority it ran under.

    This is what makes P4's ALREADY_COMPLETED answerable when the request's
    ``authority_decision_ids`` differ from the completed run's: the exact-hash
    lookup misses by construction, and missing is indistinguishable from "never
    happened" unless something ranges over the work itself.
    """
    if not work_hash:
        return None
    row = conn.execute(
        "SELECT intent_hash, receipt_id, project, state, claimed_at, updated_at, work_hash "
        "FROM intent_lineages WHERE work_hash = ? AND state = 'CONSUMED' "
        "AND intent_hash IS NOT ? ORDER BY updated_at DESC LIMIT 1",
        (work_hash, exclude_intent_hash)).fetchone()
    if row is None:
        return None
    keys = ("intent_hash", "receipt_id", "project", "state", "claimed_at", "updated_at",
            "work_hash")
    return {k: (row[k] if isinstance(row, sqlite3.Row) else row[i]) for i, k in enumerate(keys)}


def set_lineage_state(conn: sqlite3.Connection, intent_hash: str, receipt_id: str,
                      state: str) -> None:
    """Keep the claim in step with the receipt's lifecycle.

    SUPERSEDED and REVOKED release the claim so the intent can be re-admitted;
    every other state — including CONSUMED — keeps it, because "this work already
    completed" is a fact a later request has to be told (P4).
    """
    if state in ("SUPERSEDED", "REVOKED"):
        release_lineage(conn, intent_hash, receipt_id)
        return
    conn.execute("UPDATE intent_lineages SET state = ?, updated_at = ? "
                 "WHERE intent_hash = ? AND receipt_id = ?",
                 (state, time.time(), intent_hash, receipt_id))


def release_lineage(conn: sqlite3.Connection, intent_hash: str, receipt_id: str) -> bool:
    """Free the claim, but only for the receipt that holds it.

    The ``receipt_id`` predicate matters: without it a late transition on a
    superseded receipt would release the *successor's* claim and reopen the
    duplicate window this table exists to close.
    """
    cur = conn.execute("DELETE FROM intent_lineages WHERE intent_hash = ? AND receipt_id = ?",
                       (intent_hash, receipt_id))
    return cur.rowcount == 1


def _now_rfc3339() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _assert_columns_match_contract() -> None:
    """The column set is the contract's required set plus its optional keys.

    Called by the unit tests, not at import: a mismatch is a contract drift that
    must fail a test, not a live server start.
    """
    from agent_crew.cea.schema import optional_fields
    expected = set(schema_required_fields()) | set(optional_fields())
    actual = set(RECEIPT_COLUMNS)
    if expected != actual:
        raise AssertionError(f"receipt store columns drifted from the frozen schema: "
                             f"missing={sorted(expected - actual)} extra={sorted(actual - expected)}")
