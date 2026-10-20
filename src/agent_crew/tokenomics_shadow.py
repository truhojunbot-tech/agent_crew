"""Read-only adapter for an external tokenomics policy contract (#342).

This package deliberately does not implement economics policy.  It validates
an external JSON contract enough to preserve its recommendation as a receipt,
then leaves the crew's baseline cascade untouched.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def quality_evidence(recommendation: Any) -> tuple[str, str | None]:
    """Normalize quota-core's optional evidence state without guessing a boolean."""
    if not isinstance(recommendation, dict) or "quality_evidence_state" not in recommendation:
        return "legacy_absent", None
    state = recommendation["quality_evidence_state"]
    if state not in ("preserving", "not_preserving", "unknown"):
        state = "unknown"
    reason = {"unknown": "quality_evidence_unknown",
              "not_preserving": "quality_evidence_not_preserving"}.get(state)
    return state, reason


def _quality_gated(recommendation: dict) -> dict:
    state, veto = quality_evidence(recommendation)
    effective = recommendation
    if veto and "recommended_max_review_fix_rounds" in recommendation:
        effective = {**recommendation, "recommended_max_review_fix_rounds": None}
    return {"recommendation": effective, "raw_recommendation": recommendation,
            "quality_evidence_state": state, "quality_veto_reason": veto}


def shadow_recommendation(task) -> dict[str, Any]:
    return shadow_recommendation_for_task_id(task.task_id)


def shadow_recommendation_for_task_id(task_id: str) -> dict[str, Any]:
    """Return a counterfactual recommendation, never an execution command.

    Missing/unreadable contracts are represented explicitly as ``baseline``;
    availability can affect observability, never admission or STOP behavior.
    """
    path = (os.getenv("AGENT_CREW_TOKENOMICS_POLICY_PATH") or "").strip()
    baseline = {"decision_source": "baseline", "policy_version": None,
                "recommendation": None, "reason": "policy_unavailable",
                "contract_sha": None}
    if not path:
        return baseline
    contract_sha = None
    try:
        with open(path, "rb") as handle:
            raw_contract = handle.read()
            contract_mtime = os.fstat(handle.fileno()).st_mtime
        contract_sha = hashlib.sha256(raw_contract).hexdigest()
        contract = json.loads(raw_contract)
        if not isinstance(contract, dict):
            return {**baseline, "contract_sha": contract_sha}
        # quota-core #80 / PR #81 v1.0 is a shadow report containing one
        # decision per task. Agent Crew consumes it as opaque evidence; it
        # never evaluates or applies the policy itself.
        if contract.get("contract_version") == "1.0" and contract.get("mode") == "shadow":
            decisions = contract.get("decisions")
            if not isinstance(decisions, list):
                return {**baseline, "contract_sha": contract_sha}
            recommendation = next(
                (item for item in decisions
                 if isinstance(item, dict) and item.get("task_id") == task_id),
                None,
            )
            if not isinstance(recommendation, dict):
                return {**baseline, "reason": "task_decision_unavailable",
                        "contract_sha": contract_sha}
            return {"decision_source": "quota_core_contract",
                    "policy_version": contract["contract_version"],
                    "produced_at": contract.get("produced_at"),
                    "contract_mtime": contract_mtime,
                    **_quality_gated(recommendation),
                    "reason": "shadow_only", "contract_sha": contract_sha}
        if "contract_version" in contract:
            logger.warning("unsupported tokenomics policy contract version %r at %s",
                           contract.get("contract_version"), path)
            return {**baseline, "reason": "contract_version_unsupported",
                    "contract_sha": contract_sha}
        if not isinstance(contract.get("recommendation"), dict):
            return {**baseline, "contract_sha": contract_sha}
        return {"decision_source": "quota_core_contract",
                "policy_version": contract.get("version"),
                **_quality_gated(contract["recommendation"]),
                "reason": "shadow_only", "contract_sha": contract_sha}
    except (OSError, ValueError, TypeError):
        return {**baseline, "contract_sha": contract_sha}
