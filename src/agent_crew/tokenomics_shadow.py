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
        if not isinstance(contract, dict) or not isinstance(contract.get("recommendation"), dict):
            return baseline
        return {"decision_source": "quota_core_contract",
                "policy_version": contract.get("version"),
                "recommendation": contract["recommendation"],
                "reason": "shadow_only"}
    except (OSError, ValueError, TypeError):
        return baseline
