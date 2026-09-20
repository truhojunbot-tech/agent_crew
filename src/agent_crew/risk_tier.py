"""Deterministic, metadata-first cascade risk policy (Council #39).

The classifier is deliberately conservative: an explicit valid operator tier
wins, clear irreversible/external keywords escalate, documentation-only work
does not consume a review/test cascade, and unfamiliar work remains Tier 1.
It is policy metadata, not an LLM judgement, so replaying a task is stable.
"""
from __future__ import annotations

import logging
import re
from typing import Mapping

logger = logging.getLogger(__name__)

TIER_0, TIER_1, TIER_2, TIER_3 = range(4)

RISK_DECLARATION_FIELDS = (
    "safety_or_live_change",
    "broad_architecture_change",
    "bounded_routine_fix",
    "human_gate_required",
)

_TIER3 = re.compile(r"\b(stop|pause|resume|merge|deploy|external\s+(?:mutation|write|api)|delete|destroy)\b", re.I)
_TIER2 = re.compile(r"\b(core|queue|pipeline|server|protocol|schema|migration|database|api|mcp|interface|auth(?:entication)?|security)\b", re.I)


def _override(context: Mapping | None):
    value = (context or {}).get("risk_tier")
    if isinstance(value, bool):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if TIER_0 <= value <= TIER_3 else None


def classify_task(description: str, context: Mapping | None = None) -> int:
    """Return Council #39 tier from durable task metadata.

    ``context.risk_tier`` is the explicit operator override. Invalid values are
    ignored rather than coerced into a potentially lower safety tier.
    """
    ctx = context or {}
    # Watch/direct enqueue callers often put the touched paths in structured
    # metadata and leave the description as a short title. Fold only declared
    # routing metadata into the deterministic classifier; never infer it from
    # cwd/provider/session state.
    path_values = []
    for key in ("changed_paths", "paths", "files", "touches"):
        value = ctx.get(key) if isinstance(ctx, Mapping) else None
        if isinstance(value, str):
            path_values.append(value)
        elif isinstance(value, (list, tuple)):
            path_values.extend(str(item) for item in value)
    text = " ".join([description or "", *path_values])
    if isinstance(ctx, Mapping) and any(ctx.get(key) is True for key in
                                        ("external_impact", "irreversible", "requires_human_gate")):
        automatic = TIER_3
    elif _TIER3.search(text):
        automatic = TIER_3
    elif _TIER2.search(text):
        automatic = TIER_2
    elif path_values and all(path.startswith("docs/") or path.endswith(".md") for path in path_values):
        automatic = TIER_0
    else:
        automatic = TIER_1
    explicit = _override(context)
    return max(automatic, explicit) if explicit is not None else automatic


def _empty_risk_declaration() -> dict:
    return {
        **{field: None for field in RISK_DECLARATION_FIELDS},
        "declaration_source": "unknown",
        "confidence": None,
    }


def _explicit_risk_declaration(context: Mapping | None) -> dict | None:
    """Return only facts an operator actually supplied, never inferred false."""
    if not isinstance(context, Mapping):
        return None
    raw = context.get("risk_declaration")
    if isinstance(raw, Mapping):
        values = {
            field: raw.get(field) if isinstance(raw.get(field), bool) else None
            for field in RISK_DECLARATION_FIELDS
        }
        if any(field in raw and isinstance(raw.get(field), bool) for field in RISK_DECLARATION_FIELDS):
            return {**values, "declaration_source": "explicit", "confidence": "high"}

    values = {field: None for field in RISK_DECLARATION_FIELDS}
    # These structured flags already mean the coordinator observed an
    # irreversible/external or human-gated operation.  They prove the positive
    # fact only; they do not prove the other risk facts false.
    if context.get("external_impact") is True or context.get("irreversible") is True:
        values["safety_or_live_change"] = True
    if context.get("requires_human_gate") is True:
        values["human_gate_required"] = True
    if any(value is True for value in values.values()):
        return {**values, "declaration_source": "explicit", "confidence": "high"}

    # A durable coordinator-supplied tier is also an explicit escalation, but
    # its lower tiers still do not establish that safety is false.
    tier = _override(context)
    if tier == TIER_3:
        values["safety_or_live_change"] = True
        values["human_gate_required"] = True
    elif tier == TIER_2:
        values["broad_architecture_change"] = True
    elif tier == TIER_1:
        values["bounded_routine_fix"] = True
    if any(value is True for value in values.values()):
        return {**values, "declaration_source": "explicit", "confidence": "high"}
    return None


def risk_declaration(description: str, context: Mapping | None = None) -> dict:
    """Produce nullable #342(C) risk facts without changing execution policy.

    Explicit declarations win.  Otherwise this records only positive signals
    from the existing deterministic classifier: a low tier is not evidence
    that a task is *not* safety-relevant, so its safety fact remains unknown.
    """
    explicit = _explicit_risk_declaration(context)
    if explicit is not None:
        return explicit

    tier = classify_task(description, context)
    values = {field: None for field in RISK_DECLARATION_FIELDS}
    if tier == TIER_3:
        values["safety_or_live_change"] = True
        values["human_gate_required"] = True
    elif tier == TIER_2:
        values["broad_architecture_change"] = True
    elif tier == TIER_0:
        values["bounded_routine_fix"] = True
    else:
        # Tier 1 is the legacy classifier's safe default, not an observed risk
        # class.  Preserve unknown rather than presenting that default as fact.
        return _empty_risk_declaration()
    return {**values, "declaration_source": "heuristic", "confidence": "low"}


def effective_fix_round_cap(context: Mapping | None, ceiling: int | None = None) -> int:
    """Apply A-4: low-risk feedback stops before it costs another full round."""
    # Pre-Council review rows have no tier metadata. Preserve their established
    # ceiling exactly; only newly-classified lineages receive the lower cap.
    if not isinstance(context, Mapping) or "risk_tier" not in context:
        return max(0, ceiling) if ceiling is not None else 3
    tier = classify_task("", context)
    # Tier 0 has no automatic review; Tier 1 receives one bounded correction.
    policy_cap = {TIER_0: 0, TIER_1: 1, TIER_2: 3, TIER_3: 3}[tier]
    return policy_cap if ceiling is None else min(max(0, ceiling), policy_cap)


def cascade_metadata(description: str, context: Mapping | None) -> dict:
    """Stable context copied to every successor in a lineage."""
    tier = classify_task(description, context)
    return {"risk_tier": tier, "risk_tier_source": "explicit" if _override(context) is not None else "metadata"}
