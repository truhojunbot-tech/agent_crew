"""#342(C): admission risk declarations are nullable, attributed evidence."""
import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


def test_explicit_risk_declaration_is_persisted_with_explicit_provenance(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(
        "risk-explicit", "implement", "change deployment", context={
            "risk_declaration": {"safety_or_live_change": True},
        },
    ))

    declared = queue.get_task_context("risk-explicit")["risk_declaration"]
    assert declared["safety_or_live_change"] is True
    assert declared["declaration_source"] == "explicit"
    assert declared["confidence"] == "high"

    receipt = queue.get_tokenomics_shadow_receipt("risk-explicit")
    evidence = json.loads(receipt["evidence_json"])
    assert evidence["risk_declaration"] == declared


def test_heuristic_architecture_signal_is_never_labelled_explicit():
    from agent_crew.risk_tier import risk_declaration

    declared = risk_declaration("Change the core queue protocol")

    assert declared["broad_architecture_change"] is True
    assert declared["safety_or_live_change"] is None
    assert declared["declaration_source"] == "heuristic"
    assert declared["confidence"] == "low"


def test_absent_risk_signal_stays_unknown_even_though_legacy_classifier_defaults_tier_one():
    from agent_crew.risk_tier import risk_declaration

    declared = risk_declaration("implement requested work")

    assert declared["safety_or_live_change"] is None
    assert declared["broad_architecture_change"] is None
    assert declared["bounded_routine_fix"] is None
    assert declared["human_gate_required"] is None
    assert declared["declaration_source"] == "unknown"


def test_low_risk_heuristic_never_fabricates_safety_false():
    from agent_crew.risk_tier import risk_declaration

    declared = risk_declaration("update docs", {"changed_paths": ["docs/guide.md"]})

    assert declared["bounded_routine_fix"] is True
    assert declared["safety_or_live_change"] is None
    assert declared["declaration_source"] == "heuristic"


def test_declaration_is_copied_to_attribution_when_dispatch_creates_that_row(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(
        "risk-attribution", "implement", "external mutation", context={"external_impact": True},
    ))
    queue.record_attribution("risk-attribution")

    attribution = queue.get_attribution("risk-attribution")
    assert attribution["safety_or_live_change"] == 1
    assert attribution["risk_declaration_source"] == "explicit"


def test_declaration_failure_cannot_veto_admission_or_completion(monkeypatch, tmp_db):
    monkeypatch.setattr(
        "agent_crew.queue.risk_declaration",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("telemetry unavailable")),
    )
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("risk-best-effort", "implement", "x"))
    queue.record_attribution("risk-best-effort")
    queue.submit_result("risk-best-effort", TaskResult("risk-best-effort", "completed", "done"))

    assert queue.get_task_status("risk-best-effort") == "completed"


def test_risk_declaration_uses_quota_core_da6779b_quality_evidence_fields(tmp_db):
    """Pin producer field names to quota-core origin/main at da6779b (#86)."""
    quota_core = Path("/home/truhojun/alfred/repos/quota-core")
    if not quota_core.is_dir():
        pytest.skip("quota-core checkout unavailable")
    sha = "da6779bc1bd5a7a75fa615e5da5eec406c90c26c"
    source = subprocess.check_output(
        ["git", "-C", str(quota_core), "show", f"{sha}:quota_core/context_economics/policy.py"],
        text=True,
    )
    expected = {
        "safety_or_live_change", "broad_architecture_change",
        "bounded_routine_fix", "human_gate_required",
    }
    assert all(f"    {field}: bool | None = None" in source for field in expected)

    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("risk-schema", "implement", "x"))
    evidence = json.loads(queue.get_tokenomics_shadow_receipt("risk-schema")["evidence_json"])
    assert expected <= set(evidence["risk_declaration"])

    # Keep the deployed #342(A) evidence document flat.  The additive
    # declaration is removed only for validation against quota-core's closed
    # quality-evidence schema; consumers still read both from one document.
    sys.path.insert(0, str(quota_core))
    try:
        from quota_core.context_economics.policy import policy_contract_schema
        quality_evidence = {key: value for key, value in evidence.items()
                            if key != "risk_declaration"}
        jsonschema.validate(quality_evidence, policy_contract_schema()["properties"]["evidence"])
    finally:
        sys.path.remove(str(quota_core))
