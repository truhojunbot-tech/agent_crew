"""Fail-closed, repository/issue-scoped lineage HUMAN_GATE at the task writer.

An automatic fix-budget refusal is persisted on its review predecessor as
context.lineage_terminal_reason.  The successor's task ID is NOT an authority
boundary: direct coordinator enqueues must see this refusal too.

Only an already-verified, intent-bound CEA owner approval receipt may override
the block.  This function never trusts task context fields as override flags.
"""
from __future__ import annotations

import sqlite3


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

    rows = conn.execute(
        "SELECT task_id, context FROM tasks "
        "WHERE project=? AND json_valid(context) "
        "AND ("
        "  (? IS NOT NULL AND pr_number=?) OR "
        "  (? IS NOT NULL AND json_type(context, '$.issue')='integer' "
        "   AND json_extract(context, '$.issue')=?)"
        ") ORDER BY created_at DESC, task_id DESC",
        (project, pr, pr, issue, issue),
    ).fetchall()
    import json
    for row in rows:
        try:
            context = json.loads(row["context"])
        except (TypeError, ValueError):
            continue
        reason = context.get("lineage_terminal_reason")
        if isinstance(reason, str) and reason.startswith(_TERMINAL_PREFIXES):
            return str(row["task_id"])
    return None
