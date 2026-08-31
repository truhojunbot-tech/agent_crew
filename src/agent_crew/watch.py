"""#224 — continuous GitHub issue ingestion / claim loop (manager watch mode).

`crew triage` is one-shot and the runtime is deliberately push-based: workers
never poll. The consequence was that filing a GitHub issue enqueued nothing at
all — someone had to hand the issue text to a management session. This module
supplies the missing control-plane hop, and only that hop:

    open issue -> discovery -> eligibility -> atomic claim -> enqueue

Everything downstream is unchanged. The watcher writes into the same task queue
`crew run` uses, and the server's existing dispatcher picks pending rows up, so
workers stay push-driven — no polling is introduced on the worker side.

## Why the claim is a SQLite row and not just a label

The requirement is that a claim survives a process restart and two managers
running at once. A GitHub label cannot do that job: `--add-label` is idempotent,
so two watchers both "succeed" and both enqueue. The arbiter is therefore the
`issue_claims` PRIMARY KEY, taken under `BEGIN IMMEDIATE` — exactly one writer
wins, and the row is on disk so a restart sees it.

The label is the *visible* half (a requirement in its own right), plus a
best-effort advisory for managers that do not share this database. It is added
after the row is won and removed whenever the claim is released.

## Why a released claim is bounded

If enqueue fails after a claim we release, so the issue is not stuck in a fake
in-progress state. But a permanently failing enqueue would then re-claim every
interval forever. `attempts` is counted per issue and a claim that has burned
`max_attempts` becomes `abandoned` — visible in the ledger, never retried on its
own. `crew claims --release N` is the deliberate way back.
"""

import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
import time
import uuid
from typing import Optional

from agent_crew.protocol import TaskRequest
from agent_crew.triage import parse_issues

logger = logging.getLogger(__name__)

#: GitHub-visible marker for "a manager has taken this issue".
CLAIM_LABEL = "agent_crew:claimed"
#: Pre-existing marker, honoured here too: the issue is finished.
DONE_LABEL = "agent_crew:done"
#: Explicit "do not auto-claim this" marker for issues held pending a human
#: decision (e.g. a design call, not a routine bug/feature). Distinct from
#: the priority-policy labels in DEFAULT_PRIORITY_RULES on purpose: a bare
#: "blocked"/"p0" label is a policy *tier* (and "blocked" maps to priority 1
#: -- claimed FIRST), which is the opposite of what an operator wants when
#: they need the watcher to leave an issue alone. This label is an
#: eligibility gate, not a priority signal, so it can never be shadowed by
#: relabelling for urgency.
HOLD_LABEL = "agent_crew:hold"

#: How many times one issue may be claimed-then-released before we stop.
DEFAULT_MAX_ATTEMPTS = 3
#: Issues claimed per cycle. One keeps the crew focused; raise for burst intake.
DEFAULT_MAX_CLAIMS = 1
#: Poll interval bounds (req 1: "configurable and bounded").
MIN_INTERVAL_SECONDS = 10.0
MAX_INTERVAL_SECONDS = 3600.0
DEFAULT_INTERVAL_SECONDS = 300.0

#: How long a row may sit in `claimed` before a peer treats it as orphaned.
#:
#: ⛔This gate is what separates "the owning watcher died" from "the owning
#:   watcher is mid-cycle". A live claim becomes `enqueued` in well under a
#:   second (the slowest step, a `gh` label write, is capped at 30s), so five
#:   minutes is far outside normal. Without the gate, two watchers would steal
#:   each other's in-flight claims and duplicate the very enqueue the ledger
#:   exists to prevent.
CLAIM_STALE_AFTER_SECONDS = 300.0

DEFAULT_BACKOFF_BASE = 30.0
DEFAULT_BACKOFF_CAP = 900.0

#: A task in one of these states no longer occupies its issue.
_TERMINAL_TASK_STATUSES = frozenset({
    "completed", "failed", "cancelled", "canceled",
    "expired", "needs_human", "timed_out", "blocked",
})

#: Claim states that mean "do not touch this issue".
_HELD_STATES = frozenset({"claimed", "enqueued", "abandoned"})

#: Explicit operator override, e.g. `priority:1`. Wins over every tier below.
_EXPLICIT_PRIORITY_RE = re.compile(r"^priority[:/\-\s]?([1-5])$", re.IGNORECASE)

#: Default policy: concrete breakage outranks feature work (req 3). The
#: *lowest* matching tier wins, so `["bug", "p0"]` is a 1.
DEFAULT_PRIORITY_RULES: tuple[tuple[frozenset, int], ...] = (
    (frozenset({"p0", "critical", "blocker", "blocked", "incident",
                "outage", "security", "data-loss"}), 1),
    (frozenset({"bug", "regression", "production", "prod", "safety",
                "p1", "high", "observability"}), 2),
    (frozenset({"p2", "enhancement", "feature", "refactor", "cleanup"}), 4),
    (frozenset({"p3", "p4", "docs", "documentation", "chore",
                "low", "question", "idea"}), 5),
)
DEFAULT_PRIORITY = 3


# ──────────────────────────────────────────────────────────────────────
# Priority policy
# ──────────────────────────────────────────────────────────────────────


def priority_for(labels, rules=DEFAULT_PRIORITY_RULES) -> int:
    """Map issue labels to a queue priority (1 = most urgent, 5 = least).

    An explicit `priority:N` label is an operator decision and overrides the
    policy outright — including downgrading a `p0`. Bare `p0`/`p1` are policy
    tiers, not overrides, so they stay overridable.
    """
    names = [str(l).strip().lower() for l in (labels or [])]
    for name in names:
        m = _EXPLICIT_PRIORITY_RE.match(name)
        if m:
            return int(m.group(1))
    label_set = set(names)
    best = None
    for tier_labels, value in rules:
        if tier_labels & label_set and (best is None or value < best):
            best = value
    return DEFAULT_PRIORITY if best is None else best


def select_candidates(issues, *, claimed, active_issues, rules=DEFAULT_PRIORITY_RULES):
    """Filter to actionable issues and order them deterministically.

    Order is `(priority, phase, issue number)` — a total order, so the same
    backlog always yields the same pick regardless of the order GitHub
    returned it in. Returns `[]` when nothing is actionable; the caller must
    never manufacture work to fill the gap (explicit non-goal of #224).
    """
    out = []
    for issue in issues:
        number = issue.get("number")
        if number in claimed or number in active_issues:
            continue
        labels = issue.get("labels") or []
        if DONE_LABEL in labels or CLAIM_LABEL in labels or HOLD_LABEL in labels:
            continue
        out.append(issue)
    out.sort(key=lambda i: (
        priority_for(i.get("labels") or [], rules),
        i.get("phase") if i.get("phase") is not None else 999,
        i.get("number") or 0,
    ))
    return out


def backoff_seconds(
    consecutive_failures: int,
    base: float = DEFAULT_BACKOFF_BASE,
    cap: float = DEFAULT_BACKOFF_CAP,
) -> float:
    """Exponential backoff for GitHub failures, clamped so the loop never spins.

    `consecutive_failures=0` is the first failure, hence `base`.
    """
    if consecutive_failures < 0:
        consecutive_failures = 0
    # Cap the exponent before shifting so a long outage can't build a huge int.
    return float(min(base * (2 ** min(consecutive_failures, 32)), cap))


def clamp_interval(seconds: float) -> float:
    """Keep the poll interval inside the documented bounds (req 1)."""
    return float(max(MIN_INTERVAL_SECONDS, min(seconds, MAX_INTERVAL_SECONDS)))


def default_owner() -> str:
    """Identity recorded on a claim — enough to tell two managers apart."""
    return f"{socket.gethostname()}:{os.getpid()}"


# ──────────────────────────────────────────────────────────────────────
# Claim ledger
# ──────────────────────────────────────────────────────────────────────


_DDL_CLAIMS = """
CREATE TABLE IF NOT EXISTS issue_claims (
    repo         TEXT    NOT NULL,
    issue_number INTEGER NOT NULL,
    task_id      TEXT    NOT NULL DEFAULT '',
    state        TEXT    NOT NULL DEFAULT 'claimed',
    owner        TEXT    NOT NULL DEFAULT '',
    reason       TEXT    NOT NULL DEFAULT '',
    attempts     INTEGER NOT NULL DEFAULT 0,
    claimed_at   REAL    NOT NULL,
    updated_at   REAL    NOT NULL,
    PRIMARY KEY (repo, issue_number)
)
"""


class ClaimLedger:
    """Durable, atomic record of which issues this crew has taken.

    Lives in the same SQLite file as the task queue so a claim and the task it
    produced cannot drift apart across a restart.
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        conn = self._connect()
        try:
            conn.execute(_DDL_CLAIMS)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        # `isolation_level=None` = autocommit, so the explicit `BEGIN IMMEDIATE`
        # below is the only transaction in play. Leaving the driver's implicit
        # transaction management on would make the boundary of the claim's
        # critical section depend on statement sniffing.
        conn = sqlite3.connect(
            self._db_path, timeout=10, check_same_thread=False, isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        return conn

    def try_claim(
        self,
        repo: str,
        number: int,
        owner: str = "",
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> bool:
        """Take the issue, or report that someone else already has it.

        ★This is the mutex. `BEGIN IMMEDIATE` takes the write lock before the
        SELECT, so two watchers racing on the same file serialise here rather
        than both reading "free" and both inserting.
        """
        conn = self._connect()
        now = time.time()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state, attempts FROM issue_claims "
                "WHERE repo=? AND issue_number=?",
                (repo, number),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO issue_claims "
                    "(repo, issue_number, task_id, state, owner, reason, "
                    " attempts, claimed_at, updated_at) "
                    "VALUES (?, ?, '', 'claimed', ?, '', 0, ?, ?)",
                    (repo, number, owner, now, now),
                )
                conn.execute("COMMIT")
                return True
            if row["state"] in _HELD_STATES:
                conn.execute("ROLLBACK")
                return False
            # state == 'released': retry, but only within the attempt budget.
            if row["attempts"] >= max_attempts:
                conn.execute(
                    "UPDATE issue_claims SET state='abandoned', updated_at=? "
                    "WHERE repo=? AND issue_number=?",
                    (now, repo, number),
                )
                conn.execute("COMMIT")
                logger.warning(
                    "watch: issue %s#%s abandoned after %s failed attempts — "
                    "use `crew claims --release %s` to retry it deliberately",
                    repo, number, row["attempts"], number,
                )
                return False
            conn.execute(
                "UPDATE issue_claims SET state='claimed', owner=?, updated_at=? "
                "WHERE repo=? AND issue_number=?",
                (owner, now, repo, number),
            )
            conn.execute("COMMIT")
            return True
        except sqlite3.OperationalError as exc:
            # Lock contention past the busy timeout — treat as a lost race.
            # Losing a claim is always safe; winning one twice is not.
            logger.info("watch: claim contention on %s#%s: %s", repo, number, exc)
            return False
        finally:
            conn.close()

    def mark_enqueued(self, repo: str, number: int, task_id: str) -> None:
        self._set(repo, number, "task_id=?, state='enqueued'", (task_id,))

    def finish_claim(self, repo: str, number: int, task_id: str) -> bool:
        """`claimed` → `enqueued`, only if still `claimed` (compare-and-set).

        Used by reconciliation to adopt a task that a dead watcher had already
        enqueued. Conditional so two reconcilers racing on the same orphan
        cannot both "recover" it.
        """
        return self._set(
            repo, number, "task_id=?, state='enqueued'", (task_id,),
            require_state="claimed",
        )

    def release_stale(self, repo: str, number: int, reason: str = "") -> bool:
        """`claimed` → `released` (+1 attempt), only if still `claimed`.

        The attempt counter is deliberately incremented: a watcher that dies
        deterministically on one issue would otherwise re-claim it every cycle
        forever. Counting it means the existing budget parks the issue as
        `abandoned` and `crew claims` surfaces it.
        """
        return self._set(
            repo, number, "state='released', reason=?, attempts=attempts+1",
            (reason,), require_state="claimed",
        )

    def release(self, repo: str, number: int, reason: str = "") -> None:
        """Give the issue back and count the attempt.

        Called when a claim could not be turned into a queued task. Counting
        here (not on claim) is what stops a permanently failing issue from
        being re-claimed forever.
        """
        self._set(
            repo, number,
            "state='released', reason=?, attempts=attempts+1", (reason,),
        )

    def force_release(self, repo: str, number: int, reason: str = "manual") -> bool:
        """Operator escape hatch: hand an issue back to the pool (req 5).

        A task that fails terminally leaves its claim in `enqueued`, and the
        watcher deliberately will not re-take it — that is what stops an
        infinite re-run loop. This is how a human undoes that decision, and it
        resets `attempts` so the issue gets a clean budget.
        """
        return self._set(
            repo, number, "state='released', reason=?, attempts=0", (reason,),
        )

    def _set(self, repo: str, number: int, assignment: str, params: tuple,
             *, require_state: str = "") -> bool:
        guard = " AND state=?" if require_state else ""
        tail = (require_state,) if require_state else ()
        conn = self._connect()
        try:
            cur = conn.execute(
                f"UPDATE issue_claims SET {assignment}, updated_at=? "
                f"WHERE repo=? AND issue_number=?{guard}",
                (*params, time.time(), repo, number, *tail),
            )
            return cur.rowcount > 0
        except sqlite3.OperationalError as exc:
            logger.warning("watch: ledger update failed for %s#%s: %s",
                           repo, number, exc)
            return False
        finally:
            conn.close()

    def get(self, repo: str, number: int) -> Optional[dict]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM issue_claims WHERE repo=? AND issue_number=?",
                (repo, number),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def held_numbers(
        self, repo: str, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> set:
        """Issue numbers this crew must not pick up again.

        Held = actively claimed, already enqueued, abandoned, or released but
        out of attempts. Read on every cycle, which is what makes a restart a
        no-op rather than a duplicate.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT issue_number, state, attempts FROM issue_claims WHERE repo=?",
                (repo,),
            ).fetchall()
        finally:
            conn.close()
        held = set()
        for r in rows:
            if r["state"] in _HELD_STATES:
                held.add(r["issue_number"])
            elif r["state"] == "released" and r["attempts"] >= max_attempts:
                held.add(r["issue_number"])
        return held

    def list_claims(self, repo: str = "") -> list:
        conn = self._connect()
        try:
            if repo:
                rows = conn.execute(
                    "SELECT * FROM issue_claims WHERE repo=? ORDER BY issue_number",
                    (repo,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM issue_claims ORDER BY repo, issue_number"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


# ──────────────────────────────────────────────────────────────────────
# GitHub seam
# ──────────────────────────────────────────────────────────────────────


class GhCli:
    """Default gateway — shells out to `gh`, like the rest of the codebase.

    Every method is small and side-effect-explicit so tests can substitute a
    fake without patching subprocess.
    """

    def __init__(self, timeout: float = 30.0):
        self._timeout = timeout

    def list_issues(self, repo: str) -> list:
        result = subprocess.run(
            ["gh", "issue", "list", "--repo", repo, "--state", "open",
             "--limit", "200", "--json", "number,title,labels,body,url"],
            capture_output=True, text=True, timeout=self._timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"gh issue list failed ({result.returncode}): "
                f"{(result.stderr or '').strip()[:300]}"
            )
        return json.loads(result.stdout or "[]")

    def add_label(self, repo: str, number: int, label: str) -> bool:
        return self._label(repo, number, label, add=True)

    def remove_label(self, repo: str, number: int, label: str) -> bool:
        return self._label(repo, number, label, add=False)

    def _label(self, repo: str, number: int, label: str, *, add: bool) -> bool:
        flag = "--add-label" if add else "--remove-label"
        try:
            result = subprocess.run(
                ["gh", "issue", "edit", str(number), "--repo", repo, flag, label],
                capture_output=True, text=True, timeout=self._timeout,
            )
            return result.returncode == 0
        except Exception as exc:  # noqa: BLE001 — label IO must not crash the loop
            logger.warning("watch: %s %s on %s#%s failed: %s",
                           flag, label, repo, number, exc)
            return False

    def issue_has_open_pr(self, repo: str, number: int) -> bool:
        """Best-effort check for an open PR that references this issue.

        ⛔Fails *open* (returns False, "no PR known") rather than stalling
        intake on a `gh` hiccup. Duplicate work is already prevented by the
        ledger, so the cheaper failure mode here is to keep the loop alive.
        """
        try:
            result = subprocess.run(
                ["gh", "pr", "list", "--repo", repo, "--state", "open",
                 "--limit", "100", "--json", "number,title,body,headRefName"],
                capture_output=True, text=True, timeout=self._timeout,
            )
            if result.returncode != 0:
                return False
            prs = json.loads(result.stdout or "[]")
        except Exception:  # noqa: BLE001
            return False
        needle = re.compile(rf"#{number}\b")
        branch_needle = re.compile(rf"(^|[/_-]){number}([/_-]|$)")
        for pr in prs:
            if needle.search(pr.get("title") or "") or needle.search(pr.get("body") or ""):
                return True
            if branch_needle.search(pr.get("headRefName") or ""):
                return True
        return False


# ──────────────────────────────────────────────────────────────────────
# Cycle
# ──────────────────────────────────────────────────────────────────────


def active_issue_numbers(queue, repo: str = "") -> set:
    """Issues that already have a non-terminal task in the queue.

    The ledger covers issues *this* watcher took; this covers tasks created by
    any other path (`crew run`, a manual enqueue, the one-shot `crew triage`).
    """
    out = set()
    try:
        rows = queue.list_all_with_status()
    except Exception:  # noqa: BLE001 — never let bookkeeping break the cycle
        return out
    for row in rows or []:
        context = row.get("context") or {}
        number = context.get("issue")
        if not isinstance(number, int):
            continue
        row_repo = context.get("repo") or ""
        if repo and row_repo and row_repo != repo:
            continue
        if (row.get("status") or "") in _TERMINAL_TASK_STATUSES:
            continue
        out.add(number)
    return out


def tasks_by_issue(queue, repo: str = "") -> Optional[dict]:
    """`{issue number: task_id}` for **every** task, terminal ones included.

    ⛔Distinct from `active_issue_numbers`, which excludes terminal tasks. For
      reconciliation "did a task ever get created for this issue?" is the
      question — a task that ran and failed still means the enqueue happened,
      and re-doing it would resurrect the infinite re-run loop the attempt
      budget exists to prevent.

    Returns None (not `{}`) if the queue cannot be read, so callers can tell
    "no tasks" apart from "no idea" and decline to act on the latter.
    """
    try:
        rows = queue.list_all_with_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("watch: cannot read tasks for reconciliation: %s", exc)
        return None
    out: dict = {}
    for row in rows or []:
        context = row.get("context") or {}
        number = context.get("issue")
        if not isinstance(number, int):
            continue
        row_repo = context.get("repo") or ""
        if repo and row_repo and row_repo != repo:
            continue
        out.setdefault(number, row.get("task_id") or "")
    return out


def reconcile_claims(
    queue,
    ledger: ClaimLedger,
    repo: str,
    *,
    stale_after: float = CLAIM_STALE_AFTER_SECONDS,
    now: Optional[float] = None,
) -> dict:
    """Recover claims orphaned by a watcher that died mid-sequence (#224 r1).

    `try_claim` COMMITs, and only then do the label write, the enqueue and
    `mark_enqueued` run — three separate operations. The `try`/`except` arms
    around them handle *exceptions*, but not the process simply disappearing
    (SIGKILL, host teardown, power loss). That leaves two windows:

        A. dead after try_claim, before enqueue
           → row stuck in `claimed`, and `held_numbers` treats `claimed` as
             held, so the issue is locked out **forever**.
        B. dead after enqueue, before mark_enqueued
           → a task exists but the ledger still says `claimed`: same lock,
             plus the claim never learns its task_id.

    ⛔Wrapping the enqueue and the state change in one transaction — the
      obvious fix — only addresses B. A happens strictly before the enqueue,
      so no transaction around it can help. Recovery has to be reconciliation,
      which covers both, so that is what this is.

    Resolution is decided by evidence, never by guessing:

        task exists for the issue  → adopt it (`claimed` → `enqueued`).
                                     No duplicate is created.
        no task exists             → the enqueue never happened; hand the
                                     issue back (`claimed` → `released`,
                                     +1 attempt) and drop the GitHub label.
        queue unreadable           → do nothing at all.

    Both transitions are compare-and-set on `state='claimed'`, so two
    reconcilers racing on the same orphan cannot both act.

    Returns `{"resumed": [int], "released": [int]}`. Never raises.
    """
    out: dict = {"resumed": [], "released": []}
    now = time.time() if now is None else now
    try:
        rows = ledger.list_claims(repo)
    except Exception as exc:  # noqa: BLE001
        logger.warning("watch: cannot read claims for reconciliation: %s", exc)
        return out

    stale = [
        r for r in rows
        if r["state"] == "claimed" and (now - (r["updated_at"] or 0)) >= stale_after
    ]
    if not stale:
        return out

    known = tasks_by_issue(queue, repo)
    if known is None:
        return out  # unreadable queue — see docstring, we do not guess

    for row in stale:
        number = row["issue_number"]
        # ⛔Branch on *presence*, not on the id being truthy. A task row with
        #   an empty id is still proof the enqueue happened; treating it as
        #   "no task" would release the claim and enqueue a second time.
        if number in known:
            if ledger.finish_claim(repo, number, known[number]):
                task_id = known[number] or "(unknown id)"
                out["resumed"].append(number)
                logger.info(
                    "watch: reconciled %s#%s — task %s already existed, "
                    "adopting it instead of re-enqueueing (#224)",
                    repo, number, task_id,
                )
        elif ledger.release_stale(
            repo, number, reason="reconciled: interrupted before enqueue",
        ):
            out["released"].append(number)
            # ⛔The label is deliberately NOT cleared here. `reconcile_labels`
            #   owns that, runs immediately after, and retries every cycle —
            #   so a failure to clear can never strand the issue.
            logger.info(
                "watch: reconciled %s#%s — claimed but never enqueued, "
                "released for retry (#224)", repo, number,
            )
    return out


def reconcile_labels(gh, ledger: ClaimLedger, repo: str, issues: list) -> list:
    """Clear `agent_crew:claimed` from issues our own ledger already released.

    The ledger and the label are two systems with no shared transaction. Every
    release path writes one then the other, so *either* order leaves a window:
    if the process dies (or the label write fails) in between, the ledger says
    `released` — retryable — while `select_candidates` still skips the issue
    for carrying the label. Retryable in theory, invisible in practice.

    ⛔No ordering of the two writes can fix that; two independent systems
      cannot be made atomic by rearranging them. So the divergence is repaired
      instead, every cycle, from the labels we just fetched.

    ⛔★Only rows **this** ledger has in `released` are touched. A claim label
      on an issue we have no row for belongs to another manager — that label
      is the only cross-database signal between crews that do not share this
      SQLite file, and stripping it because we do not recognise it would hand
      one issue to two crews at once.

    ⚠️`abandoned` rows keep their label on purpose: `held_numbers` already
      blocks them, so nothing is stranded, and the label correctly advertises
      that agent_crew is parked on the issue.

    On success the label is also dropped from the in-memory issue, so the
    freed issue is selectable in this same cycle rather than the next one. If
    GitHub refuses the removal the issue stays skipped — claiming it while a
    peer can still see our stale label would be worse than waiting.

    Returns the issue numbers cleared. Never raises.
    """
    try:
        states = {r["issue_number"]: r["state"] for r in ledger.list_claims(repo)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("watch: cannot read claims for label reconcile: %s", exc)
        return []
    cleared = []
    for issue in issues:
        number = issue.get("number")
        labels = issue.get("labels") or []
        if CLAIM_LABEL not in labels or states.get(number) != "released":
            continue
        try:
            removed = gh.remove_label(repo, number, CLAIM_LABEL)
        except Exception as exc:  # noqa: BLE001
            logger.warning("watch: stale label clear failed for %s#%s: %s",
                           repo, number, exc)
            continue
        if not removed:
            logger.info(
                "watch: %s#%s is released but still carries %s and GitHub "
                "refused the removal — leaving it skipped until that works",
                repo, number, CLAIM_LABEL,
            )
            continue
        issue["labels"] = [l for l in labels if l != CLAIM_LABEL]
        cleared.append(number)
        logger.info(
            "watch: cleared stale %s from %s#%s — the claim was already "
            "released, the label would have hidden it from discovery (#224)",
            CLAIM_LABEL, repo, number,
        )
    return cleared


def unblocked(issues: list, all_issues: list) -> list:
    """Drop issues whose declared parent is still open.

    ⛔The open set is computed from the *full* fetch, not the filtered one.
    Otherwise a parent that is merely claimed (and therefore filtered out of
    the candidate list) would read as "resolved" and release its children
    early.
    """
    open_numbers = {
        i["number"] for i in all_issues
        if DONE_LABEL not in (i.get("labels") or [])
    }
    return [
        issue for issue in issues
        if not any(
            p in open_numbers and p != issue["number"]
            for p in (issue.get("parents") or [])
        )
    ]


def build_task(issue: dict, repo: str, branch: str, project: str = "",
               rules=DEFAULT_PRIORITY_RULES) -> TaskRequest:
    """Turn a selected issue into a queue task, preserving its provenance.

    The `context` carries enough for downstream review/PR output to link back
    to the source issue (req 4).
    """
    number = issue["number"]
    title = issue.get("title") or f"issue #{number}"
    return TaskRequest(
        task_id=f"impl-watch-{uuid.uuid4().hex[:8]}",
        task_type="implement",
        description=f"Implement #{number}: {title}",
        branch=branch,
        priority=priority_for(issue.get("labels") or [], rules),
        project=project,
        context={
            "issue": number,
            "issue_title": title,
            "issue_url": issue.get("url") or f"https://github.com/{repo}/issues/{number}",
            "repo": repo,
            "labels": list(issue.get("labels") or []),
            "source": "watch",
        },
    )


def run_cycle(
    *,
    queue,
    ledger: ClaimLedger,
    repo: str,
    gh=None,
    branch: str = "main",
    owner: str = "",
    project: str = "",
    max_claims: int = DEFAULT_MAX_CLAIMS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    reconcile_after: float = CLAIM_STALE_AFTER_SECONDS,
    rules=DEFAULT_PRIORITY_RULES,
) -> dict:
    """One reconcile→discovery→claim→enqueue pass. Never raises.

    Returns `{"enqueued": [int], "released": [int], "skipped": [int],
    "reconciled_resumed": [int], "reconciled_released": [int],
    "error": str | None}`. A non-None `error` means GitHub was unreachable
    this cycle — and because discovery happens before any claim, an error
    cycle cannot leave a half-claimed issue behind.
    """
    gh = gh if gh is not None else GhCli()
    owner = owner or default_owner()
    out: dict = {"enqueued": [], "released": [], "skipped": [],
                 "reconciled_resumed": [], "reconciled_released": [],
                 "reconciled_labels": [], "error": None}


    # Discovery and parsing share one guard: both happen strictly before any
    # claim, so whatever goes wrong here cannot strand an issue.
    try:
        raw = gh.list_issues(repo)
        all_issues = parse_issues(raw or [])
        # `parse_issues` drops `url`; carry it back so the task can link out.
        urls = {i.get("number"): i.get("url")
                for i in (raw or []) if isinstance(i, dict)}
        for issue in all_issues:
            if urls.get(issue["number"]):
                issue["url"] = urls[issue["number"]]
    except Exception as exc:  # noqa: BLE001 — transient GitHub/network/shape failure
        out["error"] = str(exc) or exc.__class__.__name__
        logger.warning("watch: issue discovery failed for %s: %s", repo, exc)
        return out

    # Recovery runs before anything reads `held_numbers` or the labels, so an
    # issue freed here is claimable in this same cycle rather than the next.
    # Every cycle, not just at startup, so a *peer* watcher's death is
    # recovered too.
    recovered = reconcile_claims(queue, ledger, repo, stale_after=reconcile_after)
    out["reconciled_resumed"] = recovered["resumed"]
    out["reconciled_released"] = recovered["released"]

    # Then repair ledger/label divergence, using the labels just fetched —
    # including rows the step above just released. Without this, an issue
    # whose claim was released but whose label survived is retryable in the
    # ledger yet permanently invisible to `select_candidates`.
    out["reconciled_labels"] = reconcile_labels(gh, ledger, repo, all_issues)

    candidates = select_candidates(
        unblocked(all_issues, all_issues),
        claimed=ledger.held_numbers(repo, max_attempts=max_attempts),
        active_issues=active_issue_numbers(queue, repo),
        rules=rules,
    )

    for issue in candidates:
        if len(out["enqueued"]) >= max_claims:
            break
        number = issue["number"]

        # An open PR means the work exists already. Checked last of the
        # filters because it is the only per-issue network call.
        try:
            if gh.issue_has_open_pr(repo, number):
                out["skipped"].append(number)
                continue
        except Exception as exc:  # noqa: BLE001
            logger.info("watch: PR check failed for %s#%s: %s", repo, number, exc)

        if not ledger.try_claim(repo, number, owner=owner, max_attempts=max_attempts):
            out["skipped"].append(number)
            continue

        # Claim is ours; make it visible before enqueueing so a crash between
        # the two leaves a *visible* claim rather than a silent one.
        try:
            labelled = gh.add_label(repo, number, CLAIM_LABEL)
        except Exception as exc:  # noqa: BLE001
            logger.warning("watch: label add failed for %s#%s: %s", repo, number, exc)
            labelled = False
        if not labelled:
            # Treat "unknown" the same as "failed" — an invisible claim is
            # exactly the state req 2 exists to prevent.
            ledger.release(repo, number, reason="claim label failed")
            out["released"].append(number)
            continue

        try:
            task_id = queue.enqueue(build_task(issue, repo, branch, project, rules))
        except Exception as exc:  # noqa: BLE001 — enqueue must not strand a claim
            logger.warning("watch: enqueue failed for %s#%s: %s", repo, number, exc)
            _safe_remove_label(gh, repo, number)
            ledger.release(repo, number, reason=f"enqueue failed: {exc}")
            out["released"].append(number)
            continue

        ledger.mark_enqueued(repo, number, task_id)
        out["enqueued"].append(number)
        logger.info("watch: enqueued %s for %s#%s", task_id, repo, number)

    return out


def _safe_remove_label(gh, repo: str, number: int) -> None:
    try:
        gh.remove_label(repo, number, CLAIM_LABEL)
    except Exception as exc:  # noqa: BLE001
        logger.warning("watch: label remove failed for %s#%s: %s", repo, number, exc)


def watch(
    *,
    queue,
    ledger: ClaimLedger,
    repo: str,
    gh=None,
    branch: str = "main",
    owner: str = "",
    project: str = "",
    interval: float = DEFAULT_INTERVAL_SECONDS,
    max_cycles: int = 0,
    max_claims: int = DEFAULT_MAX_CLAIMS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    reconcile_after: float = CLAIM_STALE_AFTER_SECONDS,
    rules=DEFAULT_PRIORITY_RULES,
    sleep_fn=time.sleep,
    on_cycle=None,
) -> dict:
    """Long-running manager loop. `max_cycles=0` runs until interrupted.

    A failing cycle backs off exponentially instead of hammering GitHub; a
    healthy cycle resets the backoff. Nothing is claimed on a failed fetch, so
    backoff can never strand a claim.
    """
    interval = clamp_interval(interval)
    gh = gh if gh is not None else GhCli()
    owner = owner or default_owner()
    failures = 0
    cycles = 0
    totals = {"cycles": 0, "enqueued": [], "released": [], "recovered": [],
              "errors": 0}

    while True:
        cycles += 1
        result = run_cycle(
            queue=queue, ledger=ledger, repo=repo, gh=gh, branch=branch,
            owner=owner, project=project, max_claims=max_claims,
            max_attempts=max_attempts, reconcile_after=reconcile_after,
            rules=rules,
        )
        totals["cycles"] = cycles
        totals["enqueued"].extend(result["enqueued"])
        totals["released"].extend(result["released"])
        totals["recovered"].extend(
            result["reconciled_resumed"] + result["reconciled_released"])
        if result["error"]:
            totals["errors"] += 1
            delay = backoff_seconds(failures)
            failures += 1
        else:
            failures = 0
            delay = interval
        if on_cycle is not None:
            on_cycle(cycles, result, delay)
        if max_cycles and cycles >= max_cycles:
            break
        sleep_fn(delay)

    return totals
