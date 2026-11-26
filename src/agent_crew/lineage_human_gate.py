"""Fail-closed, repository/issue-scoped lineage HUMAN_GATE at the task writer.

An automatic fix-budget refusal is persisted on its review predecessor as
context.lineage_terminal_reason.  The successor's task ID is NOT an authority
boundary: direct coordinator enqueues must see this refusal too.

A verified CEA owner receipt or the bounded technical exception in authority
amendment 405-v1 may override the block. Usage is counted from durable events.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time

from agent_crew.risk_tier import risk_declaration


_TERMINAL_PREFIXES = (
    "successor_refused: fix_rounds_exhausted",
    "successor_refused: fix_grant_exhausted",
)


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
    if pr is None and issue is None:
        return None

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

    # Follow shared PR/issue anchors to closure: changing PR then omitting the
    # issue must not reset the allowance. Unknown legacy metadata stays blocked.
    candidates = conn.execute(
        "SELECT * FROM tasks WHERE project=? ORDER BY created_at DESC, task_id DESC",
        (project,),
    ).fetchall()
    prs, issues = {pr} - {None}, {issue} - {None}
    rows = []
    linked_ids = set()
    link_keys = ("prev_task_id", "parent_task_id", "lineage_root_id", "root_task_id")
    while True:
        added = False
        for row in candidates:
            if any(old["task_id"] == row["task_id"] for old, _ in rows):
                continue
            try:
                ctx = json.loads(row["context"])
            except (TypeError, ValueError):
                ctx = {}
            if not isinstance(ctx, dict):
                ctx = {}
            row_issue = ctx.get("issue", ctx.get("issue_number"))
            row_pr = row["pr_number"]
            links = {ctx[key] for key in link_keys
                     if isinstance(ctx.get(key), str) and ctx[key]}
            if (row_pr in prs or (type(row_issue) is int and row_issue in issues)
                    or row["task_id"] in linked_ids or links & linked_ids):
                rows.append((row, ctx))
                linked_ids.update(links | {row["task_id"]})
                if type(row_pr) is int:
                    prs.add(row_pr)
                if type(row_issue) is int:
                    issues.add(row_issue)
                added = True
        if not added:
            break
    blocker = next((str(row["task_id"]) for row, ctx in rows
                    if isinstance(ctx.get("lineage_terminal_reason"), str)
                    and ctx["lineage_terminal_reason"].startswith(_TERMINAL_PREFIXES)), None)
    if blocker is None:
        return None

    def deny(reason):
        if refusal is not None:
            refusal["reason"] = reason
        return blocker

    context = task_context or {}
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
    if not isinstance(grant.get("finding"), str) or not re.fullmatch(
            r"(?:c|issuecomment-)?[1-9][0-9]*", grant["finding"]):
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
    if "task_exec_events" not in tables:
        return deny("extra_round_usage_unknown")
    ids = {row["task_id"] for row, _ in rows}
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
                      or fields.get("blocking_task_id") in ids))):
            return deny("extra_round_already_used")
    # The queue owns BEGIN IMMEDIATE and commits this with INSERT INTO tasks.
    # Associate evidence with the durable predecessor as well as the new task.
    conn.execute(
        "INSERT INTO task_exec_events (task_id, event, at, fields) VALUES (?, ?, ?, ?)",
        (blocker, "lineage_extra_round_admitted", time.time(), json.dumps({
            **grant, "project": project, "admitted_task_id": task_id,
            "blocking_task_id": blocker,
        })),
    )
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
