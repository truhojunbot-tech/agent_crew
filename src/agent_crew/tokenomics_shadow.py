"""Read-only adapter for an external tokenomics policy contract (#342).

This package deliberately does not implement economics policy.  It validates
an external JSON contract enough to preserve its recommendation as a receipt,
then leaves the crew's baseline cascade untouched.
"""
from __future__ import annotations

import json
import os
from typing import Any


def shadow_recommendation(task) -> dict[str, Any]:
    """Return a counterfactual recommendation, never an execution command.

    Missing/unreadable contracts are represented explicitly as ``baseline``;
    availability can affect observability, never admission or STOP behavior.
    """
    path = (os.getenv("AGENT_CREW_TOKENOMICS_POLICY_PATH") or "").strip()
    baseline = {"decision_source": "baseline", "policy_version": None,
                "recommendation": None, "reason": "policy_unavailable"}
    if not path:
        return baseline
    try:
        with open(path, encoding="utf-8") as handle:
            contract = json.load(handle)
        if not isinstance(contract, dict):
            return baseline
        # quota-core #80 / PR #81 v1.0 is a shadow report containing one
        # decision per task. Agent Crew consumes it as opaque evidence; it
        # never evaluates or applies the policy itself.
        if contract.get("contract_version") == "1.0" and contract.get("mode") == "shadow":
            decisions = contract.get("decisions")
            if not isinstance(decisions, list):
                return baseline
            recommendation = next(
                (item for item in decisions
                 if isinstance(item, dict) and item.get("task_id") == task.task_id),
                None,
            )
            if not isinstance(recommendation, dict):
                return baseline
            return {"decision_source": "quota_core_contract",
                    "policy_version": contract["contract_version"],
                    "recommendation": recommendation,
                    "reason": "shadow_only"}
        if not isinstance(contract.get("recommendation"), dict):
            return baseline
        return {"decision_source": "quota_core_contract",
                "policy_version": contract.get("version"),
                "recommendation": contract["recommendation"],
                "reason": "shadow_only"}
    except (OSError, ValueError, TypeError):
        return baseline
