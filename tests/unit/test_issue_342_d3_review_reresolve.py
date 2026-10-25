"""#342 D3: a later independent verdict can update a reviewed receipt."""

import json
import time
from datetime import datetime, timezone

import pytest

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


def _contract(path, task_id, state, produced_at):
    path.write_text(json.dumps({
        "contract_version": "1.0", "mode": "shadow", "produced_at": produced_at,
        "decisions": [{"task_id": task_id, "quality_evidence_state": state,
                       "recommended_max_review_fix_rounds": 2}],
    }))


def _after(seconds=2):
    return datetime.fromtimestamp(time.time() + seconds, timezone.utc).isoformat()


def _setup(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    policy.write_text('{"contract_version":"1.0","mode":"shadow","decisions":[]}')
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("fix-d3", "implement", "fix", branch="agent/d3"))
    queue.record_attribution("fix-d3")
    queue.record_required_context_recalled("fix-d3", True)
    queue.submit_result("fix-d3", TaskResult("fix-d3", "completed", "fixed"))
    _contract(policy, "fix-d3", "unknown", _after(0))
    queue._refresh_shadow_after_commit("fix-d3")
    assert queue.get_tokenomics_shadow_receipt("fix-d3")["quality_evidence_state"] == "unknown"
    return queue, policy


def _review(queue, name, verdict):
    queue.enqueue(TaskRequest(name, "review", "review", branch="agent/d3",
                              context={"prev_task_id": "fix-d3"}))
    queue.record_attribution(name)
    queue.submit_result(name, TaskResult(name, "completed", "reviewed", verdict=verdict))


@pytest.mark.parametrize("verdict,state", [
    ("approve", "preserving"), ("request_changes", "not_preserving"),
])
def test_late_contract_follows_review_verdict(tmp_path, monkeypatch, verdict, state):
    queue, policy = _setup(tmp_path, monkeypatch)
    _review(queue, "review-d3", verdict)
    receipt = queue.get_tokenomics_shadow_receipt("fix-d3")
    assert json.loads(receipt["evidence_json"])["independent_review_correct"] is (verdict == "approve")
    assert receipt["quality_evidence_state"] == "unknown"
    assert receipt["evidence_updated_at"] > receipt["shadow_resolved_at"]

    _contract(policy, "fix-d3", state, _after(0))
    assert queue.reresolve_tokenomics_shadow_receipts() >= 1
    receipt = queue.get_tokenomics_shadow_receipt("fix-d3")
    assert receipt["quality_evidence_state"] == state
    assert receipt["shadow_resolved_at"] >= receipt["evidence_updated_at"]


def test_later_review_overwrites_prior_quality_state(tmp_path, monkeypatch):
    queue, policy = _setup(tmp_path, monkeypatch)
    _review(queue, "review-1", "request_changes")
    _contract(policy, "fix-d3", "not_preserving", _after(0))
    queue.reresolve_tokenomics_shadow_receipts()
    assert queue.get_tokenomics_shadow_receipt("fix-d3")["quality_evidence_state"] == "not_preserving"

    _review(queue, "review-2", "approve")
    assert queue.get_tokenomics_shadow_receipt("fix-d3")["quality_evidence_state"] == "not_preserving"
    _contract(policy, "fix-d3", "preserving", _after(0))
    queue.reresolve_tokenomics_shadow_receipts()
    receipt = queue.get_tokenomics_shadow_receipt("fix-d3")
    assert json.loads(receipt["evidence_json"])["independent_review_correct"] is True
    assert receipt["quality_evidence_state"] == "preserving"


def test_unreadable_or_pre_verdict_contract_keeps_prior_state(tmp_path, monkeypatch):
    queue, policy = _setup(tmp_path, monkeypatch)
    _review(queue, "review-d3", "approve")
    receipt = queue.get_tokenomics_shadow_receipt("fix-d3")
    stale = datetime.fromtimestamp(receipt["evidence_updated_at"] - 1,
                                   timezone.utc).isoformat()
    _contract(policy, "fix-d3", "preserving", stale)
    queue.reresolve_tokenomics_shadow_receipts()
    assert queue.get_tokenomics_shadow_receipt("fix-d3")["quality_evidence_state"] == "unknown"
    policy.write_text("unreadable")
    queue.reresolve_tokenomics_shadow_receipts()
    assert queue.get_tokenomics_shadow_receipt("fix-d3")["quality_evidence_state"] == "unknown"


def test_reviewed_refresh_failure_does_not_fail_result(tmp_path, monkeypatch, caplog):
    queue, _ = _setup(tmp_path, monkeypatch)
    original = queue._refresh_shadow_after_commit

    def unavailable(task_id, *args, **kwargs):
        if task_id == "fix-d3":
            raise OSError("policy read failed")
        return original(task_id, *args, **kwargs)

    monkeypatch.setattr(queue, "_refresh_shadow_after_commit", unavailable)
    _review(queue, "review-d3", "approve")
    assert "reviewed task shadow refresh failed" in caplog.text
    assert queue.get_result("review-d3").status == "completed"
    assert json.loads(queue.get_tokenomics_shadow_receipt("fix-d3")["evidence_json"])[
        "independent_review_correct"] is True


def test_bounded_sweep_prioritizes_updated_evidence(tmp_path, monkeypatch):
    queue, policy = _setup(tmp_path, monkeypatch)
    queue.enqueue(TaskRequest("old-baseline", "test", "older row", branch="agent/d3"))
    with queue._connect() as conn:
        conn.execute("UPDATE tokenomics_shadow_receipts SET created_at=? "
                     "WHERE task_id='old-baseline'", (time.time() - 3600,))
    _review(queue, "review-d3", "approve")
    _contract(policy, "fix-d3", "preserving", _after(0))

    assert queue.reresolve_tokenomics_shadow_receipts(limit=1) == 1
    assert queue.get_tokenomics_shadow_receipt("fix-d3")["quality_evidence_state"] == "preserving"
