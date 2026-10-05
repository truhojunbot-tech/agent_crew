"""An operator tier declares all four risk facts; inference remains nullable."""

import pytest

from agent_crew.risk_tier import risk_declaration


@pytest.mark.parametrize(
    ("tier", "flags"),
    [
        (0, (False, False, False, False)),
        (1, (False, False, True, False)),
        (2, (False, True, False, False)),
        (3, (True, False, False, True)),
    ],
)
def test_explicit_tier_declares_every_risk_fact(tier, flags):
    assert risk_declaration("implement requested work", {"risk_tier": tier}) == {
        "safety_or_live_change": flags[0],
        "broad_architecture_change": flags[1],
        "bounded_routine_fix": flags[2],
        "human_gate_required": flags[3],
        "declaration_source": "explicit",
        "confidence": "high",
    }


def test_automatic_path_keeps_unknown_facts_nullable():
    assert risk_declaration("implement requested work") == {
        "safety_or_live_change": None,
        "broad_architecture_change": None,
        "bounded_routine_fix": None,
        "human_gate_required": None,
        "declaration_source": "unknown",
        "confidence": None,
    }


@pytest.mark.parametrize("tier", [0, 1, 2])
def test_live_description_escalates_lower_explicit_tier(tier):
    declaration = risk_declaration("merge then deploy", {"risk_tier": tier})
    assert declaration == {
        "safety_or_live_change": True,
        "broad_architecture_change": tier == 2,
        "bounded_routine_fix": tier == 1,
        "human_gate_required": True,
        "declaration_source": "explicit",
        "confidence": "high",
    }


def test_explicit_risk_mapping_keeps_priority_over_tier_and_description():
    declaration = {"safety_or_live_change": False}
    assert risk_declaration(
        "merge then deploy",
        {"risk_tier": 0, "risk_declaration": declaration},
    ) == {
        "safety_or_live_change": False,
        "broad_architecture_change": None,
        "bounded_routine_fix": None,
        "human_gate_required": None,
        "declaration_source": "explicit",
        "confidence": "high",
    }
