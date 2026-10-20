"""#342 E4: quality evidence gates only the existing bounded cost actions."""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from agent_crew import tokenomics_canary as canary
from agent_crew import tokenomics_shadow
from agent_crew import pipeline
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


SHA = "a" * 40


def _policy(tmp_path, monkeypatch, task_id, state="absent", produced_at=None):
    recommendation = {"task_id": task_id, "recommended_max_review_fix_rounds": 1}
    if state != "absent":
        recommendation["quality_evidence_state"] = state
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"contract_version": "1.0", "mode": "shadow",
                                "produced_at": produced_at,
                                "decisions": [recommendation]}))
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(path))
    return recommendation


def _review(task_id="review-impl-e4-r1", *, cea_outcome=None):
    context = {"prev_task_id": "impl-e4", "pr_number": 42}
    if cea_outcome:
        context["cea_enqueue"] = {"outcome": cea_outcome, "enforced": True}
    return TaskRequest(task_id, "review", "Review PR #42", branch="feat/e4",
                       context=context)


def _standing(**_kwargs):
    return {"task_id": "review-impl-e4-r0", "verdict": "request_changes",
            "findings": ["fix this"], "findings_count": 1}


@pytest.mark.parametrize("state,applied,reason,stored_state,veto", [
    ("absent", True, "standing_request_changes_on_identical_sha", "legacy_absent", None),
    ("unknown", False, "quality_evidence_unknown", "unknown", "quality_evidence_unknown"),
    ("not_preserving", False, "quality_evidence_not_preserving", "not_preserving",
     "quality_evidence_not_preserving"),
    ("preserving", True, "standing_request_changes_on_identical_sha", "preserving", None),
    ("unrecognized", False, "quality_evidence_unknown", "unknown", "quality_evidence_unknown"),
])
def test_review_cost_action_and_receipt(tmp_path, tmp_db, monkeypatch,
                                        state, applied, reason, stored_state, veto):
    recommendation = _policy(tmp_path, monkeypatch, "review-impl-e4-r1", state)
    task = _review()
    queue = TaskQueue(tmp_db)
    queue.enqueue(task)
    decision = canary.evaluate_review_dispatch(
        task, reviewed_sha=SHA, standing_lookup=_standing, pin="impl-e4")
    queue.record_tokenomics_canary_receipt(
        task.task_id, decision_source=decision.decision_source,
        recommendation=decision.recommendation(), applied=decision.applied,
        counterfactual=decision.counterfactual, reason=decision.reason)
    receipt = queue.get_tokenomics_shadow_receipt(task.task_id)
    assert decision.applied is applied
    assert decision.reason == reason
    assert receipt["quality_evidence_state"] == stored_state
    assert receipt["quality_veto_reason"] == veto
    assert json.loads(receipt["quality_recommendation_json"]) == recommendation
    assert receipt["quality_actual_action"] == (
        "suppress_identical_sha_rereview" if applied else "baseline")
    assert receipt["canary_decision_source"] == decision.decision_source
    assert receipt["task_id"] == task.task_id


@pytest.mark.parametrize("state,expected", [
    ("absent", 1), ("preserving", 1), ("unknown", None),
    ("not_preserving", None), ("unrecognized", None), (None, None),
])
def test_round_cap_citation_keeps_baseline_when_quality_is_not_known_preserving(
        tmp_path, monkeypatch, state, expected):
    _policy(tmp_path, monkeypatch, "impl-e4", state)
    shadow = tokenomics_shadow.shadow_recommendation_for_task_id("impl-e4")
    assert shadow["recommendation"]["recommended_max_review_fix_rounds"] == expected


@pytest.mark.parametrize("state,expected", [
    ("absent", 1), ("preserving", 1), ("unknown", 3),
    ("not_preserving", 3),
])
def test_existing_round_cap_action_respects_quality(
        tmp_path, tmp_db, monkeypatch, state, expected):
    produced_at = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    _policy(tmp_path, monkeypatch, "impl-e4", state, produced_at=produced_at)
    monkeypatch.setattr(pipeline, "_round_cap_pinned", lambda *_args: True)
    implement = TaskRequest("impl-e4", "implement", "Implement", branch="feat/e4")
    review = _review()

    class Queue:
        def get_exec_state(self, _task_id):
            return {"result_posted_at": time.time() - 100}

        def get_tokenomics_shadow_receipt(self, _task_id):
            return None

    cap, _, _ = pipeline._canary_round_cap(
        {implement.task_id: implement, review.task_id: review}, review, 3, Queue())
    assert cap == expected


@pytest.mark.parametrize("outcome", ["STOP", "HUMAN_GATE"])
def test_admission_hold_outranks_preserving(tmp_path, monkeypatch, outcome):
    _policy(tmp_path, monkeypatch, "review-impl-e4-r1", "preserving")
    decision = canary.evaluate_review_dispatch(
        _review(cea_outcome=outcome), reviewed_sha=SHA,
        standing_lookup=_standing, pin="impl-e4")
    assert decision.applied is False
    assert decision.reason == "admission_hold"


def test_unavailable_later_contract_does_not_erase_observed_quality(
        tmp_path, tmp_db, monkeypatch):
    _policy(tmp_path, monkeypatch, "review-impl-e4-r1", "unknown")
    queue = TaskQueue(tmp_db)
    queue.enqueue(_review())
    before = queue.get_tokenomics_shadow_receipt("review-impl-e4-r1")
    monkeypatch.delenv("AGENT_CREW_TOKENOMICS_POLICY_PATH")
    queue._refresh_shadow_after_commit("review-impl-e4-r1")
    after = queue.get_tokenomics_shadow_receipt("review-impl-e4-r1")
    for key in ("quality_evidence_state", "quality_veto_reason",
                "quality_recommendation_json"):
        assert after[key] == before[key]
