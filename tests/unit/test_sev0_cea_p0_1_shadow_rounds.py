"""EGD Step 2 (#51 P0-1): cite the contract's round budget, enforce nothing.

The cascade's fix-round cap still comes from `CascadeContract.fix_round_cap`.
These tests pin the one property that makes a shadow step worth having: the
recommendation is *recorded next to* the cap in force, and a contract that is
absent, unreadable or silent changes neither the cap nor whether the fix task
exists. If any of that stopped holding, Step 2 would be enforcing by accident.
"""

import json
import uuid

import pytest

from agent_crew.pipeline import DEFAULT_REVIEW_FIX_MAX_ROUNDS, auto_enqueue_fix
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


def _review(q, *, prev_task_id=ROOT):
    review_id = f"review-{uuid.uuid4().hex[:8]}"
    q.enqueue(TaskRequest(task_id=review_id, task_type="review", description="review",
                          branch=BRANCH, context={"prev_task_id": prev_task_id,
                                                  "pr_number": 51}))
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
