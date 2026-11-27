"""Fail-closed, repository/issue-scoped lineage HUMAN_GATE at the task writer.

An automatic fix-budget refusal is persisted on its review predecessor as
context.lineage_terminal_reason.  The successor's task ID is NOT an authority
boundary: direct coordinator enqueues must see this refusal too.

A verified CEA owner receipt or the bounded technical exception in authority
amendment 405-v1.1 may override the block. Usage is counted from durable events.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
import sqlite3
import time

from agent_crew.risk_tier import risk_declaration
from agent_crew.task_state import TERMINAL_STATUSES


_TERMINAL_PREFIXES = (
    "successor_refused: fix_rounds_exhausted",
    "successor_refused: fix_grant_exhausted",
)

# First published authority amendment, alfred#405 c7203724080. v1.1
# explicitly preserves non-retroactivity. Existing rows are never rewritten
# to opt them in: ambiguous legacy lineages require verified owner authority.
_TECHNICAL_AUTHORITY_START = datetime(2026, 11, 26, 21, 26, 10, tzinfo=timezone.utc).timestamp()
_LINK_KEYS = ("prev_task_id", "parent_task_id", "lineage_root_id",
              "lineage_root_task_id", "root_task_id")


def _lineage_links(context):
    links = set()
    for key in _LINK_KEYS:
        if key not in context:
            continue
        value = context[key]
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            return links, "invalid_lineage_reference"
        links.add(value)
    return links, None


def _finding_id(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(?:c|issuecomment-)?([1-9][0-9]*)", value)
    return match[1] if match else None


def blocking_lineage_gate(
    conn: sqlite3.Connection,
    *,
    project: str,
    pr_number: int | None,
    issue_number: object,
    receipt: dict,
    task_context: dict | None = None,
    task_id: str = "",
    refusal: dict | None = None,
) -> str | None:
    """Return blocking predecessor task ID or None.

    A blocked PR remains blocked when a coordinator mints a new task_id.
    A new PR for the same canonical issue is also linked to its prior
    exhausted lineage.  Other projects and unrelated issues are isolated.
    """
    if not project:
        return None
    issue = issue_number if type(issue_number) is int and issue_number > 0 else None
    pr = pr_number if type(pr_number) is int and pr_number > 0 else None

    # The existing CEA gate verifies the immutable receipt before reaching
    # this function.  Neither a context claim nor a coordinator statement
    # counts as owner approval.
    reuse = receipt.get("reuse") if isinstance(receipt, dict) else None
    if (isinstance(reuse, dict)
            and receipt.get("decision") == "ALLOW"
            and reuse.get("approver_identity_verified") is True
            and isinstance(reuse.get("approved_by"), str)
            and bool(reuse["approved_by"].strip())):
        return None

    context = task_context or {}
    incoming_links, link_error = _lineage_links(context)
    blocker = task_id or "unresolved_lineage"

    def deny(reason):
        if refusal is not None:
            refusal["reason"] = reason
        return blocker

    if link_error:
        return deny(link_error)
    if pr is None and issue is None and not incoming_links:
        return None

    # Follow shared PR/issue anchors to closure: changing PR then omitting the
    # issue must not reset the allowance. Unknown legacy metadata stays blocked.
    candidates = conn.execute(
        "SELECT * FROM tasks WHERE project=? ORDER BY created_at DESC, task_id DESC",
        (project,),
    ).fetchall()
    prs, issues = {pr} - {None}, {issue} - {None}
    rows = []
    linked_ids = set(incoming_links)
    selected_ids = set()
    while True:
        added = False
        for row in candidates:
            if row["task_id"] in selected_ids:
                continue
            try:
                ctx = json.loads(row["context"])
            except (TypeError, ValueError):
                ctx = {}
            if not isinstance(ctx, dict):
                ctx = {}
            row_issue = ctx.get("issue", ctx.get("issue_number"))
            row_pr = row["pr_number"]
            links, link_error = _lineage_links(ctx)
            if (row_pr in prs or (type(row_issue) is int and row_issue in issues)
                    or row["task_id"] in linked_ids or links & linked_ids):
                if link_error:
                    return deny(link_error)
                if not ctx:
                    return deny("invalid_lineage_metadata")
                if (context.get("repo") and ctx.get("repo")
                        and context["repo"] != ctx["repo"]):
                    return deny("conflicting_lineage_repo")
                if ("issue" in ctx and "issue_number" in ctx
                        and ctx["issue"] != ctx["issue_number"]):
                    return deny("conflicting_lineage_issue")
                rows.append((row, ctx))
                selected_ids.add(row["task_id"])
                linked_ids.update(links | {row["task_id"]})
                if type(row_pr) is int:
                    prs.add(row_pr)
                if type(row_issue) is int:
                    issues.add(row_issue)
                added = True
        if not added:
            break
    if linked_ids - selected_ids:
        return deny("unresolved_lineage_reference")
    if ("issue" in context and "issue_number" in context
            and context["issue"] != context["issue_number"]):
        return deny("conflicting_lineage_issue")
    blocker = next((str(row["task_id"]) for row, ctx in rows
                    if isinstance(ctx.get("lineage_terminal_reason"), str)
                    and ctx["lineage_terminal_reason"].startswith(_TERMINAL_PREFIXES)), None)
    if blocker is None:
        return None

    grant = context.get("technical_extra_round")
    if grant is None:
        return deny("fix_budget_exhausted")
    repo = context.get("repo")
    expected = {
        "grant_kind": "technical_extra_round", "authority_version": "405-v1",
        "issue": f"{repo}#{issue}", "source": "owner:17253",
    }
    if not isinstance(grant, dict):
        return deny("invalid_technical_extra_round")
    for key, value in expected.items():
        if grant.get(key) != value:
            return deny(f"invalid_technical_extra_round:{key}")
    if (not isinstance(repo, str) or not re.fullmatch(r"[^/\s]+/[^/#\s]+", repo)
            or issue is None):
        return deny("invalid_technical_extra_round:issue")
    if grant.get("authorizer") not in ("coordinator", "alfred"):
        return deny("invalid_technical_extra_round:authorizer")
    finding = _finding_id(grant.get("finding"))
    if finding is None:
        return deny("invalid_technical_extra_round:finding")
    if not task_id:
        return deny("missing_admission_task_id")

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for row, ctx in rows:
        if ctx.get("repo") != repo:
            return deny("unknown_capability:repo")
        if ("status" in row.keys() and row["status"] in
                ("needs_human", "held", "parked", "HELD")):
            return deny("owner_gate")
        if "tokenomics_shadow_receipts" in tables:
            # A canary receipt is durable evidence of a human gate; even a
            # historical hold cannot be lifted by a technical authorization.
            held = conn.execute(
                "SELECT 1 FROM tokenomics_shadow_receipts WHERE task_id=? "
                "AND canary_cea_receipt_id IS NOT NULL",
                (row["task_id"],),
            ).fetchone()
            if held:
                return deny("owner_gate:canary_HELD")
    for ctx in [context] + [ctx for _, ctx in rows]:
        reason = _technical_refusal(ctx)
        if reason:
            return deny(reason)
    # Task creation is server-written, unlike caller context timestamps. Only
    # lineages whose entire durable history starts after publication are
    # prospectively eligible. Old/undated roots stay held even if a caller adds
    # a fresh successor and an otherwise valid grant.
    for row, ctx in rows:
        row_issue = ctx.get("issue", ctx.get("issue_number"))
        if row_issue is not None and row_issue != issue:
            return deny("conflicting_lineage_issue")
        created = row["created_at"]
        if (not isinstance(created, (int, float)) or not math.isfinite(created)
                or created < _TECHNICAL_AUTHORITY_START):
            return deny("nonretroactive_lineage")
    if "task_exec_events" not in tables:
        return deny("extra_round_usage_unknown")
    ids = {row["task_id"] for row, _ in rows}
    used = 0
    admitted_ids = []
    for event in conn.execute(
            "SELECT task_id, fields FROM task_exec_events "
            "WHERE event='lineage_extra_round_admitted'"):
        try:
            fields = json.loads(event["fields"])
        except (TypeError, ValueError):
            fields = {}
        if (event["task_id"] in ids or
                (isinstance(fields, dict) and fields.get("project") == project
                 and (fields.get("issue") == grant["issue"]
                      or fields.get("blocking_task_id") in ids
                      or fields.get("admitted_task_id") in ids))):
            used += 1
            previous_finding = _finding_id(fields.get("finding")) if isinstance(fields, dict) else None
            if previous_finding is None:
                return deny("extra_round_usage_unknown")
            admitted_id = fields.get("admitted_task_id")
            if previous_finding == finding:
                if not isinstance(admitted_id, str) or not admitted_id:
                    return deny("extra_round_usage_unknown")
                blocker = admitted_id
                if refusal is not None:
                    refusal["admitted_task_id"] = admitted_id
                return deny("extra_round_finding_reused")
            admitted_ids.append(admitted_id)
    if used >= 2:
        return deny("extra_rounds_exhausted")
    for admitted_id in admitted_ids:
        disposition = _round_disposition(conn, rows, admitted_id, tables)
        if disposition:
            if isinstance(admitted_id, str) and admitted_id:
                blocker = admitted_id
                if refusal is not None:
                    refusal["admitted_task_id"] = admitted_id
            return deny(disposition)
    # The queue owns BEGIN IMMEDIATE and commits this with INSERT INTO tasks.
    # Associate evidence with the durable predecessor as well as the new task.
    conn.execute(
        "INSERT INTO task_exec_events (task_id, event, at, fields) VALUES (?, ?, ?, ?)",
        (blocker, "lineage_extra_round_admitted", time.time(), json.dumps({
            **grant, "finding": finding, "authority_amendment": "405-v1.1",
            "project": project, "admitted_task_id": task_id,
            "blocking_task_id": blocker,
        })),
    )
    return None


def _round_disposition(conn, rows, admitted_id, tables):
    """A completed implementation with an active review is still an active round.

    Follow directed successor links only, not shared PR/issue anchors (which
    also select predecessors). All reads occur under enqueue's write lock.
    Missing disposition evidence cannot authorize another round.
    """
    by_id = {row["task_id"]: row for row, _ in rows}
    if not isinstance(admitted_id, str) or admitted_id not in by_id:
        return "extra_round_disposition_unknown"
    descendants = {admitted_id}
    while True:
        before = len(descendants)
        for row, context in rows:
            links, _ = _lineage_links(context)
            if links & descendants:
                descendants.add(row["task_id"])
        if len(descendants) == before:
            break
    for task_id in descendants:
        row = by_id[task_id]
        if "status" not in row.keys() or not row["status"]:
            return "extra_round_disposition_unknown"
        if row["status"] not in TERMINAL_STATUSES:
            return "extra_round_active"
        if "cascade_outbox" in tables and conn.execute(
                "SELECT 1 FROM cascade_outbox WHERE parent_task_id=? "
                "AND state IN ('pending', 'replaying')", (task_id,)).fetchone():
            return "extra_round_active"
    return None


def _technical_refusal(context: dict) -> str | None:
    declaration = risk_declaration("", context)
    cascade = context.get("cea_cascade")
    if (context.get("risk_tier") == 3
            or declaration.get("human_gate_required") is True
            or context.get("human_gate_required") is True
            or context.get("requires_human_gate") is True
            or context.get("owner_conflict")
            or (isinstance(cascade, dict) and cascade.get("human_gate_required") is True)
            or any(context.get(key) in ("HOLD", "STOP", "PARK", "HUMAN_GATE", "HELD")
                   for key in ("owner_gate", "owner_status", "gate", "status"))):
        return "owner_gate"
    if (declaration.get("safety_or_live_change") is True
            or context.get("external_impact") is True
            or context.get("irreversible") is True):
        return "excluded_capability"
    # Positive technical classification is required; absent/heuristic facts
    # cannot establish that the lineage is outside the owner-only classes.
    if (declaration.get("declaration_source") != "explicit"
            or declaration.get("safety_or_live_change") is not False
            or declaration.get("human_gate_required") is not False):
        return "unknown_capability"
    for key in ("capability_tags", "capabilities", "capability_class", "capability"):
        if key not in context:
            continue
        tags = context[key]
        tags = [tags] if isinstance(tags, str) else tags
        if not isinstance(tags, list):
            return "unknown_capability"
        for tag in tags:
            if tag not in ("technical", "code", "tests", "docs", "read_only", "reversible_runtime"):
                return f"excluded_or_unknown_capability:{tag}"
    return None
