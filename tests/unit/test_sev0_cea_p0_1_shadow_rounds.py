"""EGD Step 2 (#51 P0-1): cite the contract's round budget, enforce nothing.

The cascade's fix-round cap still comes from `CascadeContract.fix_round_cap`.
These tests pin the one property that makes a shadow step worth having: the
recommendation is *recorded next to* the cap in force, and a contract that is
absent, unreadable or silent changes neither the cap nor whether the fix task
exists. If any of that stopped holding, Step 2 would be enforcing by accident.
"""

import json
import sqlite3
import uuid

import pytest

from agent_crew.pipeline import (
    DEFAULT_REVIEW_FIX_MAX_ROUNDS,
    auto_enqueue_fix,
    fix_task_id,
)
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue

BRANCH = "agent/claude/51-egd-step2"
ROOT = "impl-egd-root"


def _open(pr):
    """#250's terminal-PR gate has its own coverage; these tests are about the
    citation, so the PR is simply open."""
    return "open"


@pytest.fixture
def q(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id=ROOT, task_type="implement",
                              description="the originating task", branch=BRANCH))
    return queue


def _review(q, *, prev_task_id=ROOT, review_id=None, fix_round=None):
    review_id = review_id or f"review-{uuid.uuid4().hex[:8]}"
    context = {"prev_task_id": prev_task_id, "pr_number": 51}
    if fix_round is not None:
        # The counter the previous fix round left behind. `auto_enqueue_fix`
        # reads it off the review it is answering, so this is what makes the
        # next fix round 2 rather than another round 1.
        context["fix_round"] = fix_round
    q.enqueue(TaskRequest(task_id=review_id, task_type="review", description="review",
                          branch=BRANCH, context=context))
    q.submit_result(review_id, TaskResult(
        task_id=review_id, status="completed", summary="request_changes: narrow it",
        verdict="request_changes", findings=["HIGH pipeline.py:1 - cap is uncited"],
        pr_number=51))
    return review_id


def _contract(tmp_path, monkeypatch, decisions):
    policy = tmp_path / "quota-core-v1.json"
    policy.write_text(json.dumps({"contract_version": "1.0", "mode": "shadow",
                                  "decisions": decisions}))
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    return policy


def _fix(q, fix_id):
    return {t.task_id: t for t in q.list_tasks()}[fix_id]


def test_fix_task_carries_recommended_beside_the_cap_actually_used(q, tmp_path, monkeypatch):
    """★Both numbers, in both places — a gap you cannot see is not measurable."""
    _contract(tmp_path, monkeypatch, [{"task_id": ROOT, "risk_tier": "routine",
                                       "recommended_max_review_fix_rounds": 1,
                                       "rationale": "routine change, one round pays"}])
    fix_id = auto_enqueue_fix(q, _review(q), pr_state_fn=_open)

    cited = _fix(q, fix_id).context["tokenomics_shadow"]
    assert cited["recommended_max_review_fix_rounds"] == 1
    assert cited["contract_sha"]
    assert cited["rationale"] == "routine change, one round pays"

    receipt = q.get_tokenomics_shadow_receipt(fix_id)
    assert json.loads(receipt["actual_execution_json"])["shadow_rounds_vs_cap"] == {
        "recommended": 1, "actual_cap": DEFAULT_REVIEW_FIX_MAX_ROUNDS,
    }
    # ⛔Recorded, not enforced: the cap stayed the baseline one, and the fix
    #   task exists on the same terms it would have without any contract.
    assert DEFAULT_REVIEW_FIX_MAX_ROUNDS > 1, "the fixture stops proving anything otherwise"
    assert _fix(q, fix_id).context["fix_round"] == 1


def test_the_root_decision_is_cited_not_the_review_task(q, tmp_path, monkeypatch):
    """quota-core decides per originating task. Look up the review and round 2
    silently reads as "the contract said nothing"."""
    _contract(tmp_path, monkeypatch, [
        {"task_id": ROOT, "recommended_max_review_fix_rounds": 2},
        # A decoy under the review's own id, with a different number.
        {"task_id": "review-decoy", "recommended_max_review_fix_rounds": 9},
    ])
    fix_id = auto_enqueue_fix(q, _review(q), pr_state_fn=_open)

    assert _fix(q, fix_id).context["tokenomics_shadow"][
        "recommended_max_review_fix_rounds"] == 2


@pytest.mark.parametrize("body", [None, "not json", json.dumps(
    {"contract_version": "1.0", "mode": "shadow", "decisions": []})])
def test_absent_or_unreadable_contract_leaves_the_field_null_and_the_cascade_whole(
        q, tmp_path, monkeypatch, body):
    """Fail-open: no contract, a corrupt one, and one that never heard of this
    lineage all mean the same thing — no observation, same cascade."""
    if body is None:
        monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH",
                           str(tmp_path / "does-not-exist.json"))
    else:
        policy = tmp_path / "policy.json"
        policy.write_text(body)
        monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))

    fix_id = auto_enqueue_fix(q, _review(q), pr_state_fn=_open)

    assert fix_id is not None, "a missing recommendation must not withhold the fix"
    fix = _fix(q, fix_id)
    assert fix.task_type == "implement" and fix.branch == BRANCH
    assert fix.context["fix_round"] == 1
    assert fix.context["tokenomics_shadow"]["recommended_max_review_fix_rounds"] is None
    assert json.loads(q.get_tokenomics_shadow_receipt(fix_id)["actual_execution_json"])[
        "shadow_rounds_vs_cap"] == {"recommended": None,
                                    "actual_cap": DEFAULT_REVIEW_FIX_MAX_ROUNDS}


def test_a_non_integer_recommendation_is_absent_rather_than_believed(
        q, tmp_path, monkeypatch):
    """A string or a bool is not a round budget. Coercing one would invent a
    cap the contract never stated."""
    _contract(tmp_path, monkeypatch, [{"task_id": ROOT,
                                       "recommended_max_review_fix_rounds": True}])
    fix_id = auto_enqueue_fix(q, _review(q), pr_state_fn=_open)

    assert _fix(q, fix_id).context["tokenomics_shadow"][
        "recommended_max_review_fix_rounds"] is None


def test_a_citation_failure_cannot_withhold_the_fix_task(q, monkeypatch):
    """The receipt is telemetry. Losing it loses an observation, not work."""
    monkeypatch.setattr("agent_crew.pipeline.shadow_recommendation_for_task_id",
                        lambda _task_id: (_ for _ in ()).throw(RuntimeError("policy down")))

    fix_id = auto_enqueue_fix(q, _review(q), pr_state_fn=_open)

    assert fix_id is not None
    assert "tokenomics_shadow" not in _fix(q, fix_id).context


def test_a_multi_hop_lineage_cites_the_root_not_the_nearest_ancestor(
        q, tmp_path, monkeypatch):
    """implement → review → fix → review → fix, with a DECOY at every hop.

    ★The one-hop test above cannot tell "walk to the root" apart from "read
      the parent's parent" — at round 1 they are the same task. Here they are
      three tasks apart, and every id between the second fix and ROOT carries
      a decision with a different number, so stopping anywhere short of ROOT
      cites a budget quota-core published for some other task.
    """
    review_1, review_2 = "review-hop-1", "review-hop-2"
    fix_1 = fix_task_id(review_1, 1)
    _contract(tmp_path, monkeypatch, [
        {"task_id": ROOT, "recommended_max_review_fix_rounds": 2,
         "rationale": "the only decision quota-core published for this lineage"},
        {"task_id": review_1, "recommended_max_review_fix_rounds": 11},
        {"task_id": fix_1, "recommended_max_review_fix_rounds": 12},
        {"task_id": review_2, "recommended_max_review_fix_rounds": 13},
    ])

    assert auto_enqueue_fix(q, _review(q, review_id=review_1),
                            pr_state_fn=_open) == fix_1
    # Round 2 descends from the round-1 FIX, not from ROOT: three prev_task_id
    # hops (review_2 → fix_1 → review_1 → ROOT) separate it from the decision.
    fix_2 = auto_enqueue_fix(
        q, _review(q, review_id=review_2, prev_task_id=fix_1, fix_round=1),
        pr_state_fn=_open)

    assert _fix(q, fix_2).context["fix_round"] == 2, (
        "not actually a second round — the decoys would be unreachable anyway")
    cited = _fix(q, fix_2).context["tokenomics_shadow"]
    assert cited["recommended_max_review_fix_rounds"] == 2, (
        f"cited an intermediate hop's decoy, not ROOT's decision: {cited}")
    assert cited["rationale"] == (
        "the only decision quota-core published for this lineage")
    assert json.loads(q.get_tokenomics_shadow_receipt(fix_2)["actual_execution_json"])[
        "shadow_rounds_vs_cap"] == {"recommended": 2,
                                    "actual_cap": DEFAULT_REVIEW_FIX_MAX_ROUNDS}


def test_citing_the_budget_leaves_every_other_receipt_field_byte_identical(q, tmp_db):
    """One row, several writers; Step 2 owns one key of one column.

    ⛔The receipt is shared: quota-core's enqueue-time ``recommendation_json``
      and the canary's own columns already live on it. A citation that
      rewrote — or blanked — any of them would be destroying the measurements
      it was added to sit beside, and the shadow step's whole claim is that it
      changes nothing.
    """
    fix_id = auto_enqueue_fix(q, _review(q), pr_state_fn=_open)
    seeded_actual = json.dumps({
        "cascade": "baseline", "task_type": "implement",
        "shadow_reason": "shadow_only", "rationale_ko": "라운드 예산",
        "nested": {"rounds": [1, 2], "null": None},
    })
    conn = sqlite3.connect(tmp_db)
    try:
        conn.execute(
            """UPDATE tokenomics_shadow_receipts
                  SET actual_execution_json=?, recommendation_json=?,
                      policy_version=?, canary_decision_source=?,
                      canary_recommendation_json=?, canary_applied=?,
                      canary_counterfactual=?, canary_reason=?,
                      canary_cea_receipt_id=?, canary_resolved_at=?
                WHERE task_id=?""",
            (seeded_actual, json.dumps({"risk_tier": "routine", "review_mode": "full"}),
             "1.0", "quota_core_contract", json.dumps({"suppress_review": False}),
             1, "would_have_suppressed_review", "canary_shadow_only",
             "7f2d75a6-d7ac-4b21-b567-501b376041a5", 1700000000.5, fix_id))
        conn.commit()
    finally:
        conn.close()

    before = q.get_tokenomics_shadow_receipt(fix_id)
    assert before["canary_cea_receipt_id"], "the seed never landed"

    q.record_shadow_rounds_vs_cap(fix_id, recommended=1, actual_cap=3)

    after = q.get_tokenomics_shadow_receipt(fix_id)
    # `updated_at` is the one field this call is meant to move; everything
    # else — every canary_* column, the enqueue-time recommendation, the
    # task_id itself — must come back byte-for-byte.
    assert {k for k in before if before[k] != after[k]} == {
        "actual_execution_json", "updated_at"}
    assert after["updated_at"] >= before["updated_at"]

    before_actual = json.loads(before["actual_execution_json"])
    after_actual = json.loads(after["actual_execution_json"])
    assert after_actual == {**before_actual, "shadow_rounds_vs_cap": {
        "recommended": 1, "actual_cap": 3}}
    # Appended, not rebuilt: pre-existing keys keep their values AND their order.
    assert list(after_actual) == list(before_actual) + ["shadow_rounds_vs_cap"]
