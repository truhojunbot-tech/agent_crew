"""A stale switch-on rounds citation waits for the next contract (#591)."""

import json
from datetime import datetime, timezone

import pytest

from agent_crew import pipeline
from agent_crew.pipeline import auto_enqueue_fix, reresolve_pending_rounds_caps
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.tokenomics_canary import CANARY_ENV, ROUNDS_CAP_ENV


@pytest.fixture
def pending(tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, "impl-591")
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    monkeypatch.setenv("ROUNDS_CAP_RERESOLVE_WINDOW_SECONDS", "120")
    policy = tmp_path / "policy.json"
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(task_id="impl-591", task_type="implement",
                              description="root", branch="feature"))
    queue.submit_result("impl-591", TaskResult(
        task_id="impl-591", status="completed", summary="done"))
    queue.enqueue(TaskRequest(task_id="review-591", task_type="review",
                              description="review", branch="feature",
                              context={"prev_task_id": "impl-591", "fix_round": 1,
                                       "pr_number": 591, "repo": "owner/repo"}))
    queue.submit_result("review-591", TaskResult(
        task_id="review-591", status="completed", summary="changes required",
        verdict="request_changes", findings=["Fix it"], pr_number=591))
    verdict_at = queue.get_exec_state("review-591")["result_posted_at"]
    _contract(policy, verdict_at - 1)
    assert auto_enqueue_fix(queue, "review-591", repo="owner/repo",
                            pr_state_fn=lambda _: "open") is None
    assert queue.get_task(pipeline.fix_task_id("review-591", 2)) is None
    assert queue.get_tokenomics_shadow_receipt("impl-591")["canary_reason"] == (
        "switch_on:pending_reresolve")
    return queue, policy, verdict_at


def _contract(path, produced_at, *, task_id="impl-591"):
    path.write_text(json.dumps({
        "contract_version": "1.0", "mode": "shadow",
        "produced_at": datetime.fromtimestamp(produced_at, timezone.utc).isoformat(),
        "decisions": [{"task_id": task_id,
                       "recommended_max_review_fix_rounds": 1}],
    }))


def _clock(monkeypatch, when):
    real_datetime = datetime

    class Clock(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.fromtimestamp(when, tz or timezone.utc)

    monkeypatch.setattr(pipeline, "datetime", Clock)


def test_fresh_contract_cuts_without_enqueuing_fix(pending, monkeypatch):
    queue, policy, verdict_at = pending
    assert auto_enqueue_fix(queue, "review-591", repo="owner/repo",
                            pr_state_fn=lambda _: "open") is None
    _contract(policy, verdict_at + 60)
    _clock(monkeypatch, verdict_at + 61)
    monkeypatch.setattr(pipeline, "_skip_terminal_pr", lambda *a, **k: False)
    restarted = TaskQueue(queue.db_path)
    assert reresolve_pending_rounds_caps(restarted, now=verdict_at + 61) == 1
    assert queue.get_task(pipeline.fix_task_id("review-591", 2)) is None
    receipt = queue.get_tokenomics_shadow_receipt("impl-591")
    assert receipt["canary_applied"] == 1
    assert receipt["canary_reason"] == "round_cap_reached"
    assert reresolve_pending_rounds_caps(restarted, now=verdict_at + 62) == 0


def test_next_review_cited_emission_cuts_with_default_wait(pending, monkeypatch):
    queue, policy, verdict_at = pending
    monkeypatch.delenv("ROUNDS_CAP_RERESOLVE_WINDOW_SECONDS")
    assert pipeline._rounds_cap_wait_seconds() == 90
    _contract(policy, verdict_at + 60, task_id="review-591")
    _clock(monkeypatch, verdict_at + 61)
    monkeypatch.setattr(pipeline, "_skip_terminal_pr", lambda *a, **k: False)

    assert reresolve_pending_rounds_caps(queue, now=verdict_at + 61) == 1
    assert queue.get_task(pipeline.fix_task_id("review-591", 2)) is None
    receipt = queue.get_tokenomics_shadow_receipt("impl-591")
    assert receipt["canary_applied"] == 1
    assert json.loads(receipt["canary_recommendation_json"])["cited_task_id"] == "review-591"


def test_default_wait_times_out_with_baseline_and_reason(pending, monkeypatch):
    queue, _, verdict_at = pending
    monkeypatch.delenv("ROUNDS_CAP_RERESOLVE_WINDOW_SECONDS")
    _clock(monkeypatch, verdict_at + 91)
    monkeypatch.setattr(pipeline, "_skip_terminal_pr", lambda *a, **k: False)

    assert reresolve_pending_rounds_caps(queue, now=verdict_at + 91) == 1
    assert queue.get_task(pipeline.fix_task_id("review-591", 2)) is not None
    receipt = queue.get_tokenomics_shadow_receipt("impl-591")
    assert receipt["canary_applied"] == 0
    assert receipt["canary_reason"] == "contract_wait_timeout"
    counterfactual = json.loads(receipt["canary_counterfactual"])
    assert counterfactual["counterfactual_cap"] == counterfactual["baseline_cap"]
    assert counterfactual["stale_reason"] == "contract_predates_latest_fix"
    assert counterfactual["fallback_reason"] == "contract_predates_latest_result"
    assert counterfactual["wait_timed_out"] is True


def test_timeout_enqueues_at_baseline_with_explicit_reason(pending, monkeypatch):
    queue, _, verdict_at = pending
    _clock(monkeypatch, verdict_at + 121)
    monkeypatch.setattr(pipeline, "_skip_terminal_pr", lambda *a, **k: False)
    restarted = TaskQueue(queue.db_path)
    assert reresolve_pending_rounds_caps(restarted, now=verdict_at + 121) == 1
    assert queue.get_task(pipeline.fix_task_id("review-591", 2)) is not None
    receipt = queue.get_tokenomics_shadow_receipt("impl-591")
    assert receipt["canary_reason"] == "contract_wait_timeout"
    assert receipt["canary_applied"] == 0
    assert reresolve_pending_rounds_caps(restarted, now=verdict_at + 122) == 0


def test_terminal_pr_does_not_replay_unfinishable_cascade_forever(pending, monkeypatch):
    queue, _, verdict_at = pending
    _clock(monkeypatch, verdict_at + 121)
    gate_calls = []

    def terminal_pr(*_args, **_kwargs):
        gate_calls.append(True)
        return True

    monkeypatch.setattr(pipeline, "_skip_terminal_pr", terminal_pr)
    for tick in range(5):
        reresolve_pending_rounds_caps(TaskQueue(queue.db_path),
                                     now=verdict_at + 121 + tick)

    assert len(gate_calls) == 3
    assert queue.pending_rounds_cap_reresolutions() == []
    assert queue.get_task(pipeline.fix_task_id("review-591", 2)) is None
    counterfactual = json.loads(queue.get_tokenomics_shadow_receipt(
        "impl-591")["canary_counterfactual"])
    assert counterfactual["cascade_attempts"] == 3
    assert counterfactual["cascade_replay_exhausted"] is True
