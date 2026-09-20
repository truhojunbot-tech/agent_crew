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
