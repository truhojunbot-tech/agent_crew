"""One task state machine: the transition table and its single writer (#1081).

A task's ``status`` used to be written from ~two dozen ``UPDATE tasks SET
status=...`` statements, each with its own guard (or none). The same class of
lifecycle bug kept coming back as a new issue because fixing one writer left
its siblings unchanged.

This module is the target shape:

* :data:`TABLE` lists every allowed status change as data — the event name,
  the statuses it may start from, the statuses it may produce, the
  preconditions the writer enforces, the columns it writes and the context
  keys it writes or clears.
* :func:`transition` is the one writer: a single compare-and-set ``UPDATE``
  inside the caller's transaction. A refused change records a typed
  ``illegal_transition`` / ``stale_transition`` row in ``task_exec_events``
  and returns a refusal; it never raises into the caller.

Slice a (this file's first version) changes no behaviour. Every row
reproduces what its current writer does today, including the writers that
move a terminal row back to a non-terminal status. Those are *legacy
exceptions*, not successor events: each row lists the exact ``(from, to)``
pairs it may reopen in ``legacy_reopens`` and any other terminal ->
non-terminal pair is refused. Slice a does **not** establish the invariant
"a terminal row only moves on through a successor event" — there is no
successor event yet; it only pins today's exceptions so they cannot grow
silently. Only the
cancel group is routed through :func:`transition` so far (``routed=True``);
the other writers still issue their own SQL and are pinned by the allowlist in
``tests/unit/test_issue_1081_single_status_writer.py``. Later slices move them
here and shrink that list to zero.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

#: Every value ``tasks.status`` takes today.
STATUSES = frozenset({
    "pending", "in_progress", "completed", "failed", "cancelled", "timed_out",
    "needs_human", "blocked", "waiting_pool", "orphaned",
})

#: Mirrors ``queue.TERMINAL_TASK_STATUSES`` (a unit test pins the equality;
#: importing it here would make queue.py -> task_state.py circular once the
#: other writers move).
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "timed_out"})

ACTIVE_STATUSES = STATUSES - TERMINAL_STATUSES

#: The result statuses a worker or server may post (protocol.py).
RESULT_STATUSES = frozenset({"completed", "failed", "needs_human", "timed_out", "blocked"})

ILLEGAL_TRANSITION = "illegal_transition"
STALE_TRANSITION = "stale_transition"


@dataclass(frozen=True)
class Transition:
    """One row of the table: what ``event`` may do to a task's status."""

    event: str
    from_statuses: frozenset
    to_statuses: frozenset
    #: ``file:function`` of every writer that performs this event today.
    writers: tuple
    #: What the writer checks before the status write (prose, for now).
    preconditions: tuple = ()
    #: Columns written together with ``status``.
    set_columns: tuple = ()
    #: ``tasks.context`` keys this event writes (kept otherwise).
    context_written: tuple = ()
    #: ``tasks.context`` keys this event removes.
    context_cleared: tuple = ()
    #: Legacy exceptions: the exact ``(terminal_from, non_terminal_to)``
    #: pairs this event's current writer can produce. These are kept so slice
    #: a changes no behaviour; they are not successor events. Any other
    #: terminal -> non-terminal pair is refused even when both statuses are
    #: in ``from_statuses`` / ``to_statuses``.
    legacy_reopens: frozenset = frozenset()
    #: True once every writer of this event goes through :func:`transition`.
    routed: bool = False
    notes: str = ""

    @property
    def reopens_terminal(self) -> bool:
        return bool(self.legacy_reopens)

    def allows(self, from_status: str, to_status: str) -> bool:
        if from_status not in self.from_statuses or to_status not in self.to_statuses:
            return False
        if is_reopen(from_status, to_status):
            return (from_status, to_status) in self.legacy_reopens
        return True


def is_reopen(from_status: str, to_status: str) -> bool:
    """A terminal row moved back to a non-terminal status."""
    return from_status in TERMINAL_STATUSES and to_status not in TERMINAL_STATUSES


def _row(event: str, frm: Iterable[str], to: Iterable[str], writers: Iterable[str],
         *, legacy_reopens: Iterable[tuple] = (), **kw) -> Transition:
    return Transition(event=event, from_statuses=frozenset(frm),
                      to_statuses=frozenset(to), writers=tuple(writers),
                      legacy_reopens=frozenset(legacy_reopens), **kw)


_ROWS = (
    # ── claim / dispatch ────────────────────────────────────────────────
    _row("claim", {"pending"}, {"in_progress"},
         ["queue.py:claim_through_gate"],
         preconditions=("caller holds BEGIN IMMEDIATE and selected the row as pending",
                        "P2 CLAIM gate proceeds (CEA)", "signed receipt verified under enforce"),
         set_columns=("last_activity_at", "claim_source")),
    _row("defer_push_delivery", {"in_progress"}, {"pending", "in_progress"},
         ["queue.py:defer_push_delivery"],
         preconditions=("row is in_progress",
                        "to=in_progress once push_refusals reaches max_refusals, else requeue gate"),
         set_columns=("context",),
         context_written=("push_refusals", "push_not_before", "push_refusal_reason")),
    _row("defer_prep_retry", {"in_progress"}, {"pending"},
         ["queue.py:defer_prep_retry"],
         preconditions=("WHERE status='in_progress'", "requeue gate admits"),
         set_columns=("context", "claim_source"),
         context_written=("prep_retry_count", "prep_retry_error", "push_not_before",
                          "push_refusal_reason")),
    _row("defer_provider_capacity", {"in_progress"}, {"pending"},
         ["queue.py:defer_provider_capacity"],
         preconditions=("WHERE status='in_progress'", "requeue gate admits"),
         set_columns=("context", "claim_source"),
         context_written=("provider_capacity_count", "provider_capacity_since",
                          "provider_capacity_blocked_at", "push_not_before",
                          "push_refusal_reason")),
    _row("requeue", {"in_progress"}, {"pending"},
         ["queue.py:requeue"],
         preconditions=("WHERE status='in_progress'", "requeue gate admits"),
         set_columns=("claim_source",)),
    _row("requeue_dispatcher_claim", {"in_progress"}, {"pending"},
         ["queue.py:requeue_dispatcher_claim"],
         preconditions=("WHERE status='in_progress' AND claim_source='dispatcher'",
                        "no dispatched exec event yet"),
         set_columns=("claim_source",)),
    _row("reset_stale", {"in_progress"}, {"pending"},
         ["queue.py:reset_stale_to_pending"],
         preconditions=("selected as in_progress with last_activity_at older than the cutoff",
                        "requeue gate admits"),
         set_columns=("claim_source", "last_activity_at")),
    # ── parking ────────────────────────────────────────────────────────
    _row("park_codex_quota", {"in_progress"}, {"blocked"},
         ["queue.py:park_codex_quota"],
         preconditions=("WHERE status='in_progress'",),
         set_columns=("claim_source", "summary", "error_info"),
         notes="error_info.codex_quota_hold marker; summary='codex_quota_exhausted'"),
    _row("resume_codex_quota", {"blocked"}, {"pending"},
         ["queue.py:resume_codex_quota"],
         preconditions=("WHERE status='blocked'", "STOP not active",
                        "error_info.codex_quota_hold marker present", "requeue gate admits"),
         set_columns=("claim_source", "context", "error_info")),
    _row("park_pool_hold", {"pending", "in_progress"}, {"waiting_pool"},
         ["queue.py:park_pool_hold"],
         preconditions=("WHERE status IN ('pending','in_progress')",),
         set_columns=("claim_source",)),
    _row("resume_pool_hold", {"waiting_pool"}, {"pending"},
         ["queue.py:resume_pool_hold"],
         preconditions=("WHERE status='waiting_pool'",),
         set_columns=("claim_source",)),
    _row("duplicate_review_block", {"in_progress"}, {"blocked"},
         ["queue.py:record_prepared_review_base"],
         preconditions=("selected as in_progress", "an earlier review of the same base exists",
                        "allow_duplicate_review is not True"),
         set_columns=("summary", "context"),
         context_written=("duplicate_review_survivor",)),
    _row("independent_review_block", {"pending"}, {"blocked"},
         ["queue.py:bind_independent_reviews", "queue.py:_block_one_independent_review"],
         preconditions=("WHERE status='pending'",),
         set_columns=("claim_source", "summary", "context", "error_info"),
         context_written=("independent_review_block", "independent_review_binding",
                          "agent_override")),
    # ── human gates ────────────────────────────────────────────────────
    _row("premerge_gate_park", STATUSES, {"needs_human"},
         ["queue.py:mark_needs_human"],
         preconditions=("row exists",),
         set_columns=("summary", "context"),
         context_written=("<caller extra keys>",),
         # mark_needs_human has no status guard: any terminal row is parked.
         legacy_reopens={(t, "needs_human") for t in TERMINAL_STATUSES},
         notes="#588: parks an already-completed task after a PRE_MERGE BLOCK"),
    _row("quarantine_legacy_row", STATUSES, {"needs_human"},
         ["queue.py:_migrate_legacy_task_rows"],
         preconditions=("schema migration found an unparseable legacy field",),
         set_columns=("quarantine_field",),
         legacy_reopens={(t, "needs_human") for t in TERMINAL_STATUSES},
         notes="one-shot migration, not a lifecycle event"),
    # ── results / failures ─────────────────────────────────────────────
    _row("result", STATUSES, RESULT_STATUSES,
         ["queue.py:submit_result"],
         preconditions=("admission rules in submit_result (undispatched, work-intent SUCCESS, "
                        "completed review replay, late results, expected_status CAS)",),
         set_columns=("summary", "verdict", "findings", "pr_number", "error_info",
                      "status_changed_at"),
         # submit_result turns a result for a cancelled/timed_out row, a
         # system-failed row or a completed->non-completed revision into a
         # late_result event without writing status. What still writes is a
         # worker-reported ``failed`` revised by a later result, and an
         # implement ``timed_out`` row whose cancelled fallback was adopted.
         legacy_reopens={(frm, to) for frm in ("failed", "timed_out")
                         for to in RESULT_STATUSES - TERMINAL_STATUSES},
         notes="late results are recorded as late_result events, not status writes"),
    _row("force_fail", {"in_progress"}, {"failed"},
         ["queue.py:force_fail"],
         preconditions=("selected as in_progress under BEGIN IMMEDIATE",),
         set_columns=("summary", "error_info")),
    _row("force_fail_pending", {"pending"}, {"failed"},
         ["queue.py:force_fail_pending"],
         preconditions=("selected as pending under BEGIN IMMEDIATE",),
         set_columns=("summary", "error_info")),
    # ── cancel group (routed in slice a) ───────────────────────────────
    _row("cancel", ACTIVE_STATUSES, {"cancelled"},
         ["queue.py:cancel_with_binding"],
         preconditions=("not terminal", "optional expected_status CAS",
                        "optional stale_before liveness CAS"),
         routed=True,
         notes="receipt REVOKED, nonces spent and the end event commit in the same txn"),
    _row("orphan_dependent", {"pending", "in_progress"}, {"orphaned"},
         ["queue.py:cancel_with_binding"],
         preconditions=("context.prev_task_id is the task being cancelled",),
         routed=True),
    _row("owner_readmission_retire", {"needs_human"}, {"cancelled"},
         ["queue.py:_finish_owner_readmission"],
         preconditions=("context.owner_conflict.successor_task_id names the successor",
                        "the successor row exists"),
         set_columns=("summary",),
         routed=True),
)

#: event -> Transition. The table *is* this dict.
TABLE: dict = {t.event: t for t in _ROWS}
assert len(TABLE) == len(_ROWS), "duplicate event names in the transition table"


def next_statuses(from_status: str, event: str) -> frozenset:
    """Statuses ``event`` may produce from ``from_status`` (empty: refused)."""
    spec = TABLE.get(event)
    if spec is None or from_status not in spec.from_statuses:
        return frozenset()
    return spec.to_statuses


@dataclass(frozen=True)
class TransitionOutcome:
    ok: bool
    event: str
    from_status: Optional[str]
    to_status: Optional[str]
    #: None on success, else ``illegal_transition`` or ``stale_transition``.
    refusal: Optional[str] = None
    reason: str = ""
    rowcount: int = 0
    extra: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok


_COLUMN = re.compile(r"^[a-z_][a-z0-9_]*$")


def _record_refusal(conn, task_id: str, kind: str, *, event: str, expect_from,
                    to_status, actual, reason: str, at: float) -> None:
    try:
        conn.execute("SAVEPOINT task_state_refusal")
        conn.execute(
            "INSERT INTO task_exec_events (task_id, event, at, fields) VALUES (?, ?, ?, ?)",
            (task_id, kind, at, json.dumps({
                "transition_event": event, "expect_from": sorted(expect_from),
                "to_status": to_status, "actual_status": actual, "reason": reason,
            }, sort_keys=True)))
        conn.execute("RELEASE SAVEPOINT task_state_refusal")
    except Exception:
        logger.exception("task_state: %s record failed task_id=%s event=%s",
                         kind, task_id, event)
        try:
            conn.execute("ROLLBACK TO SAVEPOINT task_state_refusal")
            conn.execute("RELEASE SAVEPOINT task_state_refusal")
        except Exception:
            pass


def transition(conn, task_id: str, event: str, expect_from: Iterable[str],
               fields: Optional[Mapping[str, object]] = None, *,
               to_status: Optional[str] = None,
               now: Optional[float] = None) -> TransitionOutcome:
    """Move ``task_id`` by ``event`` with one compare-and-set ``UPDATE``.

    Runs inside the caller's connection and transaction: it neither begins nor
    commits. ``expect_from`` is the status (or statuses) the caller believes
    the row is in; the write happens only if the row is still in one of them.
    ``fields`` are the other columns written with the status; each must be
    listed in the event's ``set_columns``. ``to_status`` picks the target when
    the event has more than one.

    A refusal is returned, never raised, and leaves a typed row in
    ``task_exec_events``:

    * ``illegal_transition`` — the table does not allow it: unknown event,
      a target or expected status the event never has, an undeclared column,
      or the row is in a status this event cannot start from.
    * ``stale_transition`` — the table allows the event from the row's actual
      status but the caller expected another (it read a stale state), or the
      row no longer exists.

    The refusal row is written in the caller's transaction; a caller that
    rolls back discards it with everything else. A database error during the
    status write itself is not a refusal and is raised unchanged.
    """
    at = time.time() if now is None else now
    expect = frozenset([expect_from] if isinstance(expect_from, str) else expect_from)
    fields = dict(fields or {})
    spec = TABLE.get(event)

    def refuse(kind: str, reason: str, actual: Optional[str] = None,
               target: Optional[str] = to_status) -> TransitionOutcome:
        _record_refusal(conn, task_id, kind, event=event, expect_from=expect,
                        to_status=target, actual=actual, reason=reason, at=at)
        return TransitionOutcome(False, event, actual, target, refusal=kind, reason=reason)

    if spec is None:
        return refuse(ILLEGAL_TRANSITION, "unknown_event")
    if to_status is None:
        if len(spec.to_statuses) != 1:
            return refuse(ILLEGAL_TRANSITION, "ambiguous_target")
        (to_status,) = spec.to_statuses
    if to_status not in spec.to_statuses:
        return refuse(ILLEGAL_TRANSITION, "target_not_allowed", target=to_status)
    if not expect or not expect <= spec.from_statuses:
        return refuse(ILLEGAL_TRANSITION, "expect_from_not_allowed", target=to_status)
    if any(not spec.allows(s, to_status) for s in expect):
        return refuse(ILLEGAL_TRANSITION, "reopen_not_allowed", target=to_status)
    bad = [c for c in fields if c == "status" or not _COLUMN.match(c)
           or c not in spec.set_columns]
    if bad:
        return refuse(ILLEGAL_TRANSITION, "undeclared_column:" + ",".join(sorted(bad)),
                      target=to_status)

    cols = list(fields)
    order = sorted(expect)
    sql = ("UPDATE tasks SET status = ?" + "".join(f", {c} = ?" for c in cols)
           + " WHERE task_id = ? AND status IN (" + ", ".join("?" * len(order)) + ")")
    # A database error (lock, trigger abort, I/O) is not a refusal: it
    # propagates exactly as the caller's own UPDATE did, so its rollback and
    # retry handling stay as they are.
    rowcount = conn.execute(
        sql, (to_status, *(fields[c] for c in cols), task_id, *order)).rowcount
    if rowcount == 1:
        # Exactly one row matched, so its prior status was in ``expect``; with a
        # single expected status that is the prior status itself.
        prior = order[0] if len(order) == 1 else None
        return TransitionOutcome(True, event, prior, to_status, rowcount=1)

    row = conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        return refuse(STALE_TRANSITION, "task_missing", target=to_status)
    actual = row[0]
    if spec.allows(actual, to_status):
        return refuse(STALE_TRANSITION, "status_moved", actual=actual, target=to_status)
    return refuse(ILLEGAL_TRANSITION, "from_not_allowed", actual=actual, target=to_status)
