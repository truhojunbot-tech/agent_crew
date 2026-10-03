"""Switch-off rounds-cap receipts settle when quota-core first observes a verdict."""

import json
import time
from datetime import datetime, timezone

import pytest

from agent_crew import pipeline
from agent_crew.pipeline import auto_enqueue_fix, reresolve_pending_rounds_caps
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app
from agent_crew.tokenomics_canary import CANARY_ENV, ROUNDS_CAP_ENV
from fastapi.testclient import TestClient


@pytest.fixture
def pending(tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, "impl-reresolve")
    monkeypatch.delenv(ROUNDS_CAP_ENV, raising=False)
    policy = tmp_path / "policy.json"
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(task_id="impl-reresolve", task_type="implement",
                              description="root", branch="feature"))
    queue.submit_result("impl-reresolve", TaskResult(
        task_id="impl-reresolve", status="completed", summary="done"))
    queue.enqueue(TaskRequest(task_id="review-reresolve", task_type="review",
                              description="review", branch="feature",
                              context={"prev_task_id": "impl-reresolve", "fix_round": 1,
                                       "pr_number": 80}))
    queue.submit_result("review-reresolve", TaskResult(
        task_id="review-reresolve", status="completed", summary="changes required",
        verdict="request_changes", findings=["Fix the test failure"], pr_number=80))
    verdict_at = queue.get_exec_state("review-reresolve")["result_posted_at"]
    root_at = queue.get_exec_state("impl-reresolve")["result_posted_at"]
    _write_contract(policy, (root_at + verdict_at) / 2)
    fix = auto_enqueue_fix(queue, "review-reresolve", repo="owner/repo",
                           pr_state_fn=lambda _: "open", comment_fn=lambda *_a: None)
    assert fix is not None  # the live cascade does not wait for a new contract
    row = queue.get_tokenomics_shadow_receipt("impl-reresolve")
    assert row["canary_reason"] == "switch_off:pending_reresolve"
    assert json.loads(row["canary_counterfactual"])[
        "stale_reason"] == "contract_predates_latest_result"
    assert row["canary_applied"] == 0
    return queue, policy, verdict_at


def _write_contract(path, produced_at):
    path.write_text(json.dumps({
        "contract_version": "1.0", "mode": "shadow",
        "produced_at": datetime.fromtimestamp(produced_at, timezone.utc).isoformat(),
        "decisions": [{"task_id": "impl-reresolve",
                       "recommended_max_review_fix_rounds": 1}],
    }))


def _clock(monkeypatch, when):
    real_datetime = datetime

    class Clock(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.fromtimestamp(when, tz or timezone.utc)

    monkeypatch.setattr(pipeline, "datetime", Clock)


def test_fresh_contract_reresolves_once_with_sha_and_would_fire(pending, monkeypatch):
    queue, policy, verdict_at = pending
    _write_contract(policy, verdict_at + 30)
    _clock(monkeypatch, verdict_at + 31)
    assert reresolve_pending_rounds_caps(queue, now=verdict_at + 31) == 1
    row = queue.get_tokenomics_shadow_receipt("impl-reresolve")
    citation = json.loads(row["canary_recommendation_json"])
    counterfactual = json.loads(row["canary_counterfactual"])
    assert row["canary_reason"] == "switch_off:fresh_reresolved"
    assert citation["produced_at"] == datetime.fromtimestamp(
        verdict_at + 30, timezone.utc).isoformat()
    assert citation["contract_sha"]
    assert counterfactual["counterfactual_cap"] == 1
    assert counterfactual["would_fire"] is True
    assert row["canary_applied"] == 0
    assert reresolve_pending_rounds_caps(queue, now=verdict_at + 32) == 0
    before_replay = row["canary_counterfactual"]
    auto_enqueue_fix(queue, "review-reresolve", repo="owner/repo",
                     pr_state_fn=lambda _: "open", comment_fn=lambda *_a: None)
    replayed = queue.get_tokenomics_shadow_receipt("impl-reresolve")
    assert replayed["canary_reason"] == "switch_off:fresh_reresolved"
    assert replayed["canary_counterfactual"] == before_replay


@pytest.mark.parametrize("offset", [-1, 90])
def test_out_of_window_contract_stays_baseline(pending, monkeypatch, offset):
    queue, policy, verdict_at = pending
    _write_contract(policy, verdict_at + offset)
    _clock(monkeypatch, verdict_at + 30)
    assert reresolve_pending_rounds_caps(queue, now=verdict_at + 30) == 0
    assert queue.get_tokenomics_shadow_receipt("impl-reresolve")[
        "canary_reason"] == "switch_off:pending_reresolve"


def test_restart_after_window_finalizes_stale(pending, monkeypatch):
    queue, policy, verdict_at = pending
    restarted = TaskQueue(queue.db_path)
    _clock(monkeypatch, verdict_at + 61)
    assert reresolve_pending_rounds_caps(restarted, now=verdict_at + 61) == 1
    row = restarted.get_tokenomics_shadow_receipt("impl-reresolve")
    assert row["canary_reason"] in (
        "switch_off:contract_predates_latest_result",
        "switch_off:contract_predates_latest_fix")
    assert row["canary_applied"] == 0
    assert json.loads(row["canary_counterfactual"])["counterfactual_cap"] == 3
    assert reresolve_pending_rounds_caps(restarted, now=verdict_at + 62) == 0


def test_dispatcher_tick_reresolves_without_waiting_in_cascade(pending, monkeypatch):
    queue, policy, verdict_at = pending
    time.sleep(0.01)
    _write_contract(policy, time.time())
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", "0.05")
    app = create_app(queue.db_path, pane_map={}, worktree_map={},
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if queue.get_tokenomics_shadow_receipt("impl-reresolve")[
                    "canary_reason"] == "switch_off:fresh_reresolved":
                break
            time.sleep(0.05)
    assert queue.get_tokenomics_shadow_receipt("impl-reresolve")[
        "canary_reason"] == "switch_off:fresh_reresolved"
