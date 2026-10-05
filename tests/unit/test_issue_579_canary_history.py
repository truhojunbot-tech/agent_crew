"""#579: keep every canary evaluation on the lineage receipt."""

import json

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


def test_canary_history_keeps_both_evaluations_and_latest_columns(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("review-579", "review", "Review PR #579",
                              branch="feature/579"))
    before = queue.get_tokenomics_shadow_receipt("review-579")
    queue.record_tokenomics_canary_receipt(
        "review-579", decision_source="baseline",
        recommendation={"cap": 3}, applied=False,
        counterfactual="round=1 would_fire=false", reason="stale")
    first = queue.get_tokenomics_shadow_receipt("review-579")
    queue.record_tokenomics_canary_receipt(
        "review-579", decision_source="quota_core_contract",
        recommendation={"cap": 2}, applied=False,
        counterfactual="round=2 would_fire=true", reason="fresh",
        cea_receipt_id="receipt-2")
    row = queue.get_tokenomics_shadow_receipt("review-579")

    assert row["canary_decision_source"] == "quota_core_contract"
    assert json.loads(row["canary_recommendation_json"]) == {"cap": 2}
    assert row["canary_applied"] == 0
    assert row["canary_counterfactual"] == "round=2 would_fire=true"
    assert row["canary_reason"] == "fresh"
    assert row["canary_cea_receipt_id"] == "receipt-2"
    assert row["canary_resolved_at"] >= first["canary_resolved_at"]

    evidence = json.loads(row["evidence_json"])
    assert evidence["risk_declaration"] == json.loads(before["evidence_json"])["risk_declaration"]
    history = evidence["canary_history"]
    assert len(history) == 2
    assert history[0] == {
        "at": first["canary_resolved_at"], "decision_source": "baseline",
        "recommendation": {"cap": 3}, "applied": False,
        "counterfactual": "round=1 would_fire=false", "reason": "stale",
        "cea_receipt_id": None,
    }
    assert history[1] == {
        "at": row["canary_resolved_at"], "decision_source": "quota_core_contract",
        "recommendation": {"cap": 2}, "applied": False,
        "counterfactual": "round=2 would_fire=true", "reason": "fresh",
        "cea_receipt_id": "receipt-2",
    }
    queue.submit_result("review-579", TaskResult(
        "review-579", "completed", "approved", verdict="approve",
        findings=[], pr_number=579))
    after_completion = queue.get_tokenomics_shadow_receipt("review-579")
    assert json.loads(after_completion["evidence_json"])["canary_history"] == history


def test_canary_history_appends_even_when_preserve_applied_keeps_latest(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.record_tokenomics_canary_receipt(
        "lineage-579", decision_source="quota_core_contract",
        recommendation={"cap": 1}, applied=True,
        counterfactual="held", reason="round_cap_reached",
        cea_receipt_id="held-receipt")
    first = queue.get_tokenomics_shadow_receipt("lineage-579")
    queue.record_tokenomics_canary_receipt(
        "lineage-579", decision_source="baseline",
        recommendation={"cap": 3}, applied=False,
        counterfactual="later baseline", reason="stale",
        preserve_applied=True)
    row = queue.get_tokenomics_shadow_receipt("lineage-579")
    for column in ("canary_decision_source", "canary_recommendation_json",
                   "canary_applied", "canary_counterfactual", "canary_reason",
                   "canary_cea_receipt_id", "canary_resolved_at"):
        assert row[column] == first[column]
    history = json.loads(row["evidence_json"])["canary_history"]
    assert len(history) == 2
    assert history[0]["applied"] is True
    assert history[1]["applied"] is False
    assert history[1]["reason"] == "stale"
    assert history[1]["counterfactual"] == "later baseline"


def test_fresh_reresolve_is_retained_once_in_history(tmp_db):
    queue = TaskQueue(tmp_db)
    pending = '{"pending_reresolve": true}'
    queue.record_tokenomics_canary_receipt(
        "lineage-579", decision_source="baseline", recommendation={},
        applied=False, counterfactual=pending,
        reason="switch_off:pending_reresolve")
    changed = queue.resolve_pending_rounds_cap(
        "lineage-579", expected_counterfactual=pending,
        decision_source="quota_core_contract", recommendation={"cap": 2},
        counterfactual={"counterfactual_cap": 2, "would_fire": True},
        reason="switch_off:fresh_reresolved")
    assert changed is True
    assert queue.resolve_pending_rounds_cap(
        "lineage-579", expected_counterfactual=pending,
        decision_source="quota_core_contract", recommendation={"cap": 1},
        counterfactual={"counterfactual_cap": 1},
        reason="switch_off:fresh_reresolved") is False
    row = queue.get_tokenomics_shadow_receipt("lineage-579")
    history = json.loads(row["evidence_json"])["canary_history"]
    assert len(history) == 2
    assert history[1]["decision_source"] == "quota_core_contract"
    assert history[1]["recommendation"] == {"cap": 2}
    assert history[1]["counterfactual"] == {"counterfactual_cap": 2, "would_fire": True}
    assert history[1]["reason"] == "switch_off:fresh_reresolved"
    assert history[1]["applied"] is False


def test_promoted_canary_evaluation_is_retained(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.record_tokenomics_canary_receipt(
        "lineage-579", decision_source="baseline", recommendation={},
        applied=False, counterfactual="would run", reason="shadow")
    conn = queue._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        queue._promote_tokenomics_canary_receipt_in_txn(
            conn, "lineage-579", decision_source="quota_core_contract",
            recommendation={"kind": "suppress"}, counterfactual="review skipped",
            reason="identical_sha", cea_receipt_id="receipt-579", now=1234.0)
        conn.commit()
    finally:
        conn.close()
    history = json.loads(queue.get_tokenomics_shadow_receipt("lineage-579")["evidence_json"])["canary_history"]
    assert len(history) == 2
    assert history[1] == {
        "at": 1234.0, "decision_source": "quota_core_contract",
        "recommendation": {"kind": "suppress"}, "applied": True,
        "counterfactual": "review skipped", "reason": "identical_sha",
        "cea_receipt_id": "receipt-579",
    }


def test_held_fix_canary_evaluation_is_retained(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("root-579", "implement", "root",
                              branch="feature/579"))
    review = TaskRequest("review-579", "review", "review",
                         branch="feature/579",
                         context={"prev_task_id": "root-579", "fix_round": 1})
    queue.enqueue(review)
    receipt_id = queue.hold_tokenomics_canary_fix(
        review, receipt_task_id="root-579", fix_round=2,
        recommendation={"cap": 1},
        counterfactual={"round": 2, "would_fire": True})
    row = queue.get_tokenomics_shadow_receipt("root-579")
    history = json.loads(row["evidence_json"])["canary_history"]
    assert history[-1]["applied"] is True
    assert history[-1]["reason"] == "round_cap_reached"
    assert history[-1]["counterfactual"] == {"round": 2, "would_fire": True}
    assert history[-1]["cea_receipt_id"] == receipt_id
