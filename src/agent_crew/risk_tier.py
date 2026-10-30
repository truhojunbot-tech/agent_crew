"""Declared risk metadata for cascade decisions (Council #39)."""
from __future__ import annotations

import os
from typing import Mapping

TIER_0, TIER_1, TIER_2, TIER_3 = range(4)

RISK_DECLARATION_FIELDS = (
    "safety_or_live_change",
    "broad_architecture_change",
    "bounded_routine_fix",
    "human_gate_required",
)

def risk_tier_enforcement_enabled() -> bool:
    """Return whether the Council #39 cascade is explicitly enabled.

    The incident default is observation-only: an absent or unrecognised value
    preserves the pre-risk-tier review/test/fix cascade.
    """
    return os.getenv("AGENT_CREW_RISK_TIER_ENFORCEMENT", "").lower() in {
        "1", "true", "yes", "on",
    }


def _override(context: Mapping | None):
    value = (context or {}).get("risk_tier")
    if type(value) is not int:
        return None
    return value if TIER_0 <= value <= TIER_3 else None


def classify_task(description: str, context: Mapping | None = None) -> int:
    """Return the declared Council #39 tier; prose has no authority."""
    tier = _override(context)
    if tier is None:
        raise ValueError("context.risk_tier must be an integer 0-3")
    return tier


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
            inherited_from = raw.get("inherited_from")
            if isinstance(inherited_from, str) and inherited_from:
                return {**values, "declaration_source": "explicit", "confidence": "high",
                        "inherited_from": inherited_from}
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

    # An operator-supplied tier is a complete declaration, including the
    # absence of each risk flag and the all-false Tier 0.
    tier = _override(context)
    if tier is not None:
        return {
            "safety_or_live_change": tier == TIER_3,
            "broad_architecture_change": tier == TIER_2,
            "bounded_routine_fix": tier == TIER_1,
            "human_gate_required": tier == TIER_3,
            "declaration_source": "explicit",
            "confidence": "high",
        }
    return None


def risk_declaration(description: str, context: Mapping | None = None) -> dict:
    """Return only declared risk facts; never infer them from task prose."""
    explicit = _explicit_risk_declaration(context)
    if explicit is not None:
        return explicit
    return _empty_risk_declaration()


def effective_fix_round_cap(context: Mapping | None, ceiling: int | None = None) -> int:
    """Return the operator baseline; quota-core citation narrows it in the cascade."""
    return max(0, ceiling) if ceiling is not None else 3


def cascade_metadata(description: str, context: Mapping | None) -> dict:
    """Stable context copied to every successor in a lineage."""
    tier = classify_task(description, context)
    return {"risk_tier": tier, "risk_tier_source": "explicit"}


def shadow_decision(
    description: str,
    context: Mapping | None,
    task_id: str,
    actual_action: str,
    ceiling: int | None = None,
) -> dict:
    """Return a counterfactual safety receipt without deriving a round budget."""
    metadata = cascade_metadata(description, context)
    tier = metadata["risk_tier"]
    return {
        "task_id": task_id,
        "tier": tier,
        "tier_source": metadata["risk_tier_source"],
        "would_gate": tier == TIER_3,
        "would_test_scope": "skip" if tier == TIER_0 else "targeted" if tier == TIER_1 else "full",
        "would_fix_cap": None,
        "actual_action": actual_action,
    }
