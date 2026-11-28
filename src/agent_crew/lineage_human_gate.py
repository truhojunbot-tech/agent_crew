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
    error = None
    for key in _LINK_KEYS:
        if key not in context:
            continue
        value = context[key]
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            error = "invalid_lineage_reference"
            continue
        links.add(value)
    return links, error


def _resolved_chain(conn, project, seeds, *, validate_cycles=True):
    """Resolve persisted edges independently of shared issue/PR metadata.

    Include descendants so a marker naming a root inherits its durable refusal.
    Only persisted endpoints connect components; two missing legacy references
    are not evidence that otherwise independent tasks belong to one chain.
    """
    rows = {}
    edges = {}
    errors = {}
    for row in conn.execute("SELECT * FROM tasks WHERE project=?", (project,)):
        try:
            ctx = json.loads(row["context"])
        except (TypeError, ValueError):
            ctx = {}
        if not isinstance(ctx, dict):
            ctx = {}
        node = row["task_id"]
        links, error = _lineage_links(ctx)
        # Root self-markers are declarations, not graph edges.
        links.discard(node)
        if any(ctx.get(key) == node for key in _LINK_KEYS
               if key not in ("lineage_root_id", "lineage_root_task_id", "root_task_id")):
            links.add(node)
        rows[node] = (row, ctx)
        edges[node] = links
        errors[node] = error or ("invalid_lineage_metadata" if not ctx else None)
    reverse = {}
    for node, links in edges.items():
        for link in links:
            if link in rows:
                reverse.setdefault(link, set()).add(node)
    selected, missing = set(), set()
    pending = list(seeds)
    while pending:
        node = pending.pop()
        if node in selected or node in missing:
            continue
        if node not in rows:
            missing.add(node)
            continue
        selected.add(node)
        pending.extend(edges[node] | reverse.get(node, set()))
    # Iterative DFS distinguishes cycles from convergent root/parent markers.
    finished, active = set(), set()
    for seed in selected if validate_cycles else ():
        stack = [(seed, False)]
        while stack:
            node, leaving = stack.pop()
            if leaving:
                active.remove(node)
                finished.add(node)
            elif node in active:
                return [], "cyclic_lineage"
            elif node in selected and node not in finished:
                active.add(node)
                stack.append((node, True))
                stack.extend((link, False) for link in edges[node])
    error = next((errors[node] for node in sorted(selected) if errors[node]), None)
    if missing:
        error = "unresolved_lineage_reference"
    return [pair for node, pair in rows.items() if node in selected], error


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
    root_keys = {"lineage_root_id", "lineage_root_task_id", "root_task_id"}
    fresh_root = (pr is None and not link_error
                  and all(key in root_keys and context[key] == task_id
                          for key in _LINK_KEYS if key in context))
    if (pr is None and issue is None and not incoming_links
            and "technical_extra_round" not in context):
        return None

    # Keep the shared anchor closure for owner controls, conflict checks and
    # finding/review uniqueness. Explicit lineage resolution is separate.
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
    shared_rows = rows
    # A self-root declaration is not a persisted edge, whether or not the
    # request carries a PR. Keep non-root self-links for cycle validation.
    seeds, _ = _lineage_links({
        key: value for key, value in context.items()
        if key not in root_keys or value != task_id
    })
    rows, chain_error = _resolved_chain(conn, project, seeds)
    if chain_error == "cyclic_lineage":
        return deny(chain_error)
    # An absent direct reference is not evidence of an independent chain.
    # Preserve conservative legacy handling when nothing was resolved, but
    # never replace a real component merely because an older ancestor is absent.
    if not fresh_root and not rows:
        # Shared history is a conservative authority fallback, not evidence
        # that its unrelated components belong to the incoming lineage.
        rows, fallback_error = _resolved_chain(
            conn, project, {row["task_id"] for row, _ in shared_rows},
            validate_cycles=False)
        chain_error = chain_error or fallback_error
    metadata_error = metadata_error or chain_error
    if any(context.get("repo") and ctx.get("repo")
           and context["repo"] != ctx["repo"] for _, ctx in rows):
        metadata_error = "conflicting_lineage_repo"
    # Exhaustion stays component-scoped, but shared exhaustion tells us when
    # isolation has made an ordinary request newly eligible. That path still
    # owes the shared owner/canary checks that guarded it before isolation.
    cap_rows = {row["task_id"]: (row, ctx) for row, ctx in shared_rows + rows}
    refused_ids = {
        node for node, (row, ctx) in cap_rows.items()
        if durable_cap_refusal(conn, node)
        or (isinstance(ctx.get("lineage_terminal_reason"), str)
            and ctx["lineage_terminal_reason"].startswith(_TERMINAL_PREFIXES))
    }
    blocker = next((str(row["task_id"]) for row, _ in rows
                    if row["task_id"] in refused_ids), None)
    shared_exhausted = any(row["task_id"] in refused_ids for row, _ in shared_rows)

    def stored_pr(row, ctx):
        return row["pr_number"] or ctx.get("pr_number")

    # A real B component cannot be used to target an exhausted A PR. Changing
    # PRs remains possible unless the destination itself has exhausted history.
    if (pr is not None
            and any(row["task_id"] in refused_ids and stored_pr(row, ctx) == pr
                    for row, ctx in shared_rows)
            and not any(stored_pr(row, ctx) == pr for row, ctx in rows)):
        return deny("conflicting_lineage_pr")
    if (not fresh_root and blocker is None and not shared_exhausted
            and "technical_extra_round" not in context):
        return None
    # Shared owner authority remains issue-wide, including fresh roots;
    # unrelated capability and legacy facts do not.
    for row, _ in shared_rows:
        if conn.execute(
            "SELECT 1 FROM authorization_receipts WHERE receipt_id=? "
            "AND (decision='HUMAN_GATE' OR runtime_state IN ('HOLD', 'STOP', 'PARK') "
            "OR json_extract(human_gate_state, '$.state') IN ('PENDING', 'DENIED'))",
            (row["receipt_id"],),
        ).fetchone():
            return deny("owner_gate:receipt")
        if conn.execute(
            "SELECT 1 FROM tokenomics_shadow_receipts WHERE task_id=? "
            "AND canary_applied=1 AND canary_cea_receipt_id IS NOT NULL", (row["task_id"],),
        ).fetchone():
            return deny("owner_gate:canary_HELD")
    if blocker is None and "technical_extra_round" not in context:
        return None

    if metadata_error:
        return deny(metadata_error)
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

    by_id = {row["task_id"]: (row, ctx) for row, ctx in rows}
    review_id = context.get("prev_task_id")
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

    # Derive the root from stored predecessor edges, never the incoming claim.
    root = review_id
    ancestors = set()
    while root in by_id:
        if root in ancestors:
            return deny("cyclic_lineage")
        ancestors.add(root)
        parent = by_id[root][1].get("prev_task_id")
        if not parent:
            break
        if parent not in by_id:
            return deny("unresolved_lineage_reference")
        root = parent

    admissions = []
    for event in conn.execute(
        "SELECT task_id, fields FROM task_exec_events "
        "WHERE event='lineage_extra_round_admitted' ORDER BY event_id"
    ):
        fields = json.loads(event["fields"])
        if event["task_id"] in selected_ids:
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
    # The next refusal must descend from the last admitted round; an unrelated
    # old refusal sharing the issue cannot authorize concurrent work.
    if admissions and admissions[-1].get("admitted_task_id") not in ancestors:
        return deny("review_not_from_previous_round")

    for row, ctx in shared_rows:
        row_issue = ctx.get("issue", ctx.get("issue_number"))
        if row_issue is not None and row_issue != issue:
            return deny("conflicting_lineage_issue")
    for row, ctx in rows:
        if ctx.get("repo") != repo:
            return deny("unknown_capability:repo")
        row_issue = ctx.get("issue", ctx.get("issue_number"))
        if row_issue is not None and row_issue != issue:
            return deny("conflicting_lineage_issue")
        created = row["created_at"]
        if (not isinstance(created, (int, float)) or not math.isfinite(created)
                or created < _TECHNICAL_AUTHORITY_START):
            return deny("nonretroactive_lineage")
    for ctx in [context] + [ctx for _, ctx in rows]:
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
