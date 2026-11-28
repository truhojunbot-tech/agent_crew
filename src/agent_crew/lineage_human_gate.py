"""#1081 c-r: admission authority is the cascade's durable cap refusal.

Owner 4085: "enforce existing STOP/HOLD/HUMAN_GATE/cap, no new policy".
405-v1.1: "up to 2 extra bounded rounds, one at a time, each naming its finding".
No status, outbox, or round-disposition inference belongs in this gate.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
import time

from agent_crew.risk_tier import risk_declaration


class LineageHumanGateError(Exception):
    code = "LINEAGE_HUMAN_GATE"

    def __init__(self, blocking_task_id, reason, admitted_task_id=None):
        self.blocking_task_id = blocking_task_id
        self.reason = reason
        self.admitted_task_id = admitted_task_id
        super().__init__(f"{self.code}: {reason}; blocking_task_id={blocking_task_id}"
                         f"; admitted_task_id={admitted_task_id}")



class LineageSiblingActiveError(LineageHumanGateError):
    code = "LINEAGE_SIBLING_ACTIVE"

    def __init__(self, survivor):
        super().__init__(survivor, f"sibling_active:{survivor}", survivor)


def active_implement_sibling(conn, *, project, pr_number, task_context, task_id):
    """Identify siblings using explicit edges, never shared issue metadata.

    Called under the queue writer lock. A PR match is independently sufficient;
    without it, both the root and the review/test finding must match.
    """
    rows = conn.execute(
        "SELECT task_id, task_type, status, pr_number, context FROM tasks "
        "WHERE project=? ORDER BY created_at, task_id", (project,),
    ).fetchall()
    by_id = {}
    for row in rows:
        try:
            context = json.loads(row["context"])
        except (TypeError, ValueError):
            context = {}
        by_id[row["task_id"]] = (row, context if isinstance(context, dict) else {})

    def identity(context):
        finding = context.get("prev_task_id")
        if not isinstance(finding, str) or not finding:
            return None
        predecessor = by_id.get(finding)
        if predecessor is None or predecessor[0]["task_type"] not in ("review", "test"):
            return None
        seen = set()
        current = context
        node = None
        while True:
            root = current.get("lineage_root_task_id")
            if isinstance(root, str) and root and root != node:
                if root in seen:
                    return None
                seen.add(root)
                node = root
                ancestor = by_id.get(root)
                if ancestor is None:
                    return root, finding
                current = ancestor[1]
                continue
            parent = current.get("prev_task_id")
            if not isinstance(parent, str) or not parent:
                return (node, finding) if node else None
            if parent in seen:
                return None
            seen.add(parent)
            node = parent
            ancestor = by_id.get(parent)
            if ancestor is None:
                return None
            current = ancestor[1]

    incoming = identity(task_context)
    for row, context in by_id.values():
        if (row["task_id"] == task_id or row["task_type"] != "implement"
                or row["status"] not in (
                    "pending", "queued", "waiting_pool", "in_progress", "claimed")):
            continue
        if ((pr_number is not None and row["pr_number"] == pr_number)
                or (incoming is not None and identity(context) == incoming)):
            return row["task_id"]
    return None


_TERMINAL_PREFIXES = (
    "successor_refused: fix_rounds_exhausted",
    "successor_refused: fix_grant_exhausted",
)

# First published authority amendment, alfred#405 c7203724080. v1.1
# explicitly preserves non-retroactivity. Existing rows are never rewritten
# to opt them in: ambiguous legacy lineages require verified owner authority.
_TECHNICAL_AUTHORITY_START = datetime(2026, 11, 26, 21, 26, 10, tzinfo=timezone.utc).timestamp()
_LINK_KEYS = ("prev_task_id", "parent_task_id", "lineage_root_id",
              "lineage_root_task_id", "root_task_id", "retry_of", "fallback_of",
              "fallback_from_task_id", "original_task_id")


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


def durable_cap_refusal(conn, task_id):
    """The first cascade cap refusal survives later terminal diagnostics."""
    row = conn.execute(
        "SELECT json_extract(fields, '$.terminal_reason') AS terminal "
        "FROM task_exec_events WHERE task_id=? AND event='successor_refused' "
        "AND json_extract(fields, '$.terminal_reason') IN (?, ?) "
        "ORDER BY event_id LIMIT 1", (task_id, *_TERMINAL_PREFIXES),
    ).fetchone()
    return row["terminal"] if row else None


def admit_lineage_extra_round(conn, *, project, pr_number, issue_number,
                             task_context, task_id, verified_owner=False):
    """Check and record admission under the task writer's BEGIN IMMEDIATE."""
    if verified_owner:
        return
    issue = issue_number if type(issue_number) is int and issue_number > 0 else None
    pr = pr_number if type(pr_number) is int and pr_number > 0 else None
    context = task_context or {}
    incoming_links, link_error = _lineage_links(context)
    blocker = task_id or "unresolved_lineage"

    def deny(reason):
        raise LineageHumanGateError(blocker or task_id, reason)

    metadata_error = link_error
    # A new root has no PR or parent; its optional self root marker is not a
    # missing stored task. It still participates in shared owner-gate checks.
    root_keys = {"lineage_root_id", "lineage_root_task_id", "root_task_id"}
    fresh_root = (
        pr is None and not link_error and "technical_extra_round" not in context
        and all(key in root_keys and context[key] == task_id
                for key in _LINK_KEYS if key in context)
    )
    if (pr is None and issue is None and not incoming_links
            and "technical_extra_round" not in context):
        return None

    # Follow shared PR/issue anchors to closure for owner controls and durable
    # history. Extra-round accounting below uses the stored predecessor root.
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
                if ("issue" in ctx and "issue_number" in ctx
                        and ctx["issue"] != ctx["issue_number"]):
                    metadata_error = "conflicting_lineage_issue"
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
    if ("issue" in context and "issue_number" in context
            and context["issue"] != context["issue_number"]):
        metadata_error = "conflicting_lineage_issue"
    # Membership comes from persisted predecessor edges, not issue joins or
    # caller-declared root identities. Validate only this chain's legacy links.
    by_id = {row["task_id"]: (row, ctx) for row, ctx in rows}
    review_id = context.get("prev_task_id")
    root = review_id
    ancestors = set()
    while root is not None:
        if not isinstance(root, str) or not root.strip() or root != root.strip():
            return deny("invalid_lineage_reference")
        if root in ancestors:
            return deny("cyclic_lineage")
        if root not in by_id:
            return deny("unresolved_lineage_reference")
        ancestors.add(root)
        row, ctx = by_id[root]
        links, error = _lineage_links(ctx)
        if error:
            return deny(error)
        if not ctx:
            return deny("invalid_lineage_metadata")
        if links - by_id.keys():
            return deny("unresolved_lineage_reference")
        if (context.get("repo") and ctx.get("repo")
                and context["repo"] != ctx["repo"]):
            return deny("conflicting_lineage_repo")
        parent = ctx.get("prev_task_id")
        if parent is None:
            break
        root = parent

    # Legacy requests identifying an existing PR but no predecessor retain
    # that PR's exhaustion protection; issue-only history is not membership.
    cap_ids = ancestors if review_id is not None else {
        row["task_id"] for row, _ in rows if pr is not None and row["pr_number"] == pr
    }
    blocker = next((str(row["task_id"]) for row, ctx in rows
                    if row["task_id"] in cap_ids and (
                        durable_cap_refusal(conn, row["task_id"])
                        or (isinstance(ctx.get("lineage_terminal_reason"), str)
                            and ctx["lineage_terminal_reason"].startswith(_TERMINAL_PREFIXES)))), None)

    # A fresh root bypasses unrelated exhaustion and legacy references only,
    # never shared owner receipts or canary holds (including in CEA off mode).
    for row, _ in rows:
        if conn.execute(
            "SELECT 1 FROM authorization_receipts WHERE receipt_id=? "
            "AND (decision='HUMAN_GATE' OR runtime_state IN ('HOLD', 'STOP', 'PARK') "
            "OR json_extract(human_gate_state, '$.state') IN ('PENDING', 'DENIED'))",
            (row["receipt_id"],),
        ).fetchone():
            return deny("owner_gate:receipt")
        if conn.execute(
            "SELECT 1 FROM tokenomics_shadow_receipts WHERE task_id=? "
            "AND canary_cea_receipt_id IS NOT NULL", (row["task_id"],),
        ).fetchone():
            return deny("owner_gate:canary_HELD")
    if metadata_error:
        return deny(metadata_error)
    for _, ctx in rows:
        row_issue = ctx.get("issue", ctx.get("issue_number"))
        if row_issue is not None and issue is not None and row_issue != issue:
            return deny("conflicting_lineage_issue")
    if fresh_root or (blocker is None and "technical_extra_round" not in context):
        return None
    grant = context.get("technical_extra_round")
    if grant is None:
        return deny("fix_budget_exhausted")
    repo = context.get("repo")
    expected = {
        "grant_kind": "technical_extra_round", "authority_version": "405-v1.1",
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

    predecessor = by_id.get(review_id)
    if predecessor is None or predecessor[0]["task_type"] != "review":
        return deny("durable_refusal_required")
    terminal = predecessor[1].get("lineage_terminal_reason")
    if terminal not in _TERMINAL_PREFIXES or not conn.execute(
        "SELECT 1 FROM task_exec_events WHERE task_id=? AND event='successor_refused' "
        "AND json_extract(fields, '$.terminal_reason')=?",
        (review_id, terminal),
    ).fetchone():
        return deny("durable_refusal_required")

    admissions = []
    for event in conn.execute(
        "SELECT task_id, fields FROM task_exec_events "
        "WHERE event='lineage_extra_round_admitted' ORDER BY event_id"
    ):
        fields = json.loads(event["fields"])
        if event["task_id"] in by_id:
            if event["task_id"] == root:
                admissions.append(fields)
            survivor = fields.get("admitted_task_id")
            if fields.get("review_task_id") == review_id:
                raise LineageHumanGateError(review_id, "review_already_used", survivor)
            if _finding_id(fields.get("finding")) == finding:
                raise LineageHumanGateError(review_id, "extra_round_finding_reused", survivor)
    # Positive uniqueness guard, not a claim that child absence finishes a round.
    child = conn.execute(
        "SELECT task_id FROM tasks WHERE json_extract(context, '$.prev_task_id')=? "
        "ORDER BY created_at, task_id LIMIT 1", (review_id,),
    ).fetchone()
    if child:
        raise LineageHumanGateError(review_id, "review_already_used", child["task_id"])
    if len(admissions) >= 2:
        return deny("extra_rounds_exhausted")
    # Within this root, the next refusal must descend from the last admitted
    # round; a sibling refusal cannot authorize concurrent work.
    if admissions and admissions[-1].get("admitted_task_id") not in ancestors:
        return deny("review_not_from_previous_round")

    for row, ctx in rows:
        if row["task_id"] in ancestors and ctx.get("repo") != repo:
            return deny("unknown_capability:repo")
        row_issue = ctx.get("issue", ctx.get("issue_number"))
        if row_issue is not None and row_issue != issue:
            return deny("conflicting_lineage_issue")
        created = row["created_at"]
        if row["task_id"] in ancestors and (
                not isinstance(created, (int, float)) or not math.isfinite(created)
                or created < _TECHNICAL_AUTHORITY_START):
            return deny("nonretroactive_lineage")
    for ctx in [context] + [ctx for row, ctx in rows if row["task_id"] in ancestors]:
        reason = _technical_refusal(ctx)
        if reason:
            return deny(reason)
    conn.execute(
        "INSERT INTO task_exec_events (task_id, event, at, fields) VALUES (?, ?, ?, ?)",
        (root, "lineage_extra_round_admitted", time.time(), json.dumps({
            **grant, "finding": finding, "root_task_id": root,
            "review_task_id": review_id, "admitted_task_id": task_id,
            "project": project,
        })),
    )


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
                   for key in ("owner_gate", "owner_status"))):
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
