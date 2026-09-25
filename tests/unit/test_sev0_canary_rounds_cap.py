"""D-11832: only a fresh quota-core decision may narrow one pinned lineage."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agent_crew.pipeline import auto_enqueue_fix, auto_enqueue_test
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.tokenomics_canary import CANARY_ENV, ROUNDS_CAP_ENV

ROOT = "impl-canary-rounds"


@pytest.fixture
def q(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id=ROOT, task_type="implement",
                              description="root", branch="canary-branch"))
    return queue


def _contract(tmp_path, monkeypatch, recommended=1, *, age=0):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "contract_version": "1.0", "mode": "shadow",
        "produced_at": (datetime.now(timezone.utc) - timedelta(days=age)).isoformat(),
        "decisions": [{"task_id": ROOT,
                       "recommended_max_review_fix_rounds": recommended}],
    }))
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(path))


def _review(q, task_id="review-canary-rounds", *, root=ROOT, fix_round=1,
            verdict="request_changes"):
    q.enqueue(TaskRequest(task_id=task_id, task_type="review", description="review",
                          branch="canary-branch",
                          context={"prev_task_id": root, "fix_round": fix_round,
                                   "pr_number": 51}))
    q.submit_result(task_id, TaskResult(
        task_id=task_id, status="completed", summary="fix it", verdict=verdict,
        findings=["Fix the review finding"], pr_number=51))
    return task_id


def _run(q, review_id, comments=None):
    return auto_enqueue_fix(q, review_id, pr_state_fn=lambda _: "open",
                            comment_fn=lambda pr, body: (comments.append(body)
                                                          if comments is not None else None),
                            already_announced_fn=lambda *a, **kw: False,
                            repo="owner/repo")


def test_kill_switch_off_preserves_fix_and_shadow_receipt(q, tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.delenv(ROUNDS_CAP_ENV, raising=False)
    _contract(tmp_path, monkeypatch)
    review = _review(q)
    assert _run(q, review) is not None
    assert q.get_tokenomics_shadow_receipt(ROOT)["canary_applied"] is None


def test_narrow_cap_holds_fix_with_cea_receipt(q, tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    _contract(tmp_path, monkeypatch)
    review = _review(q)
    comments = []
    assert _run(q, review, comments) is None
    assert _run(q, review, comments) is None  # result replay must reuse the hold
    assert len(comments) == 1
    assert "human needs to decide" in comments[0]
    assert not any(t.task_id.startswith("fix-") for t in q.list_tasks())
    row = q.get_tokenomics_shadow_receipt(ROOT)
    assert row["canary_applied"] == 1
    assert json.loads(row["actual_execution_json"])["shadow_rounds_vs_cap"] == {
        "recommended": 1, "actual_cap": 1,
    }
    assert json.loads(row["canary_counterfactual"]) == {
        "baseline_cap": 3, "effective_cap": 1, "round": 2,
        "would_have_enqueued_fix": True,
    }
    assert row["canary_cea_receipt_id"]
    with sqlite3.connect(q._db_path) as conn:
        receipt = json.loads(conn.execute(
            "SELECT receipt_json FROM authorization_receipts WHERE receipt_id=? ORDER BY seq DESC LIMIT 1",
            (row["canary_cea_receipt_id"],)).fetchone()[0])
        assert receipt["decision"] == "HUMAN_GATE"
        assert receipt["reason"]["code"] == "HUMAN_GATE_PENDING"
        assert receipt["human_gate_state"] == "PENDING"
        assert receipt["state"] == "HELD"
        assert conn.execute(
            "SELECT count(*) FROM authorization_receipts WHERE receipt_id=?",
            (row["canary_cea_receipt_id"],)).fetchone()[0] == 2


@pytest.mark.parametrize("recommended,age,expected_reason", [
    (3, 0, "recommendation_does_not_narrow"),
    (1, 2, "contract_missing_or_stale"),
    (0, 0, "invalid_recommendation"),
])
def test_no_narrowing_uses_baseline(q, tmp_path, monkeypatch,
                                    recommended, age, expected_reason):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    _contract(tmp_path, monkeypatch, recommended, age=age)
    review = _review(q)
    assert _run(q, review) is not None
    row = q.get_tokenomics_shadow_receipt(ROOT)
    assert row["canary_applied"] == 0
    assert row["canary_reason"] == expected_reason


def test_other_lineage_uses_baseline(q, tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    _contract(tmp_path, monkeypatch)
    review = _review(q, root="unrelated")
    assert _run(q, review) is not None
    assert q.get_tokenomics_shadow_receipt(ROOT)["canary_applied"] is None


def test_existing_identical_sha_canary_receipt_is_preserved(q, tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    _contract(tmp_path, monkeypatch)
    review = _review(q, fix_round=0)
    q.record_tokenomics_canary_receipt(
        review, decision_source="quota_core_contract",
        recommendation={"kind": "suppress_identical_sha_rereview"},
        applied=True, counterfactual="review skipped",
        reason="standing_request_changes_on_identical_sha")
    assert _run(q, review) is not None
    assert q.get_tokenomics_shadow_receipt(review)["canary_reason"] == (
        "standing_request_changes_on_identical_sha")
    assert q.get_tokenomics_shadow_receipt(ROOT)["canary_reason"] == "cap_not_reached"


def test_cap_not_reached_keeps_review_then_test(q, tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    _contract(tmp_path, monkeypatch)
    review = _review(q, fix_round=0)
    fix = _run(q, review)
    assert fix is not None
    assert q.get_tokenomics_shadow_receipt(ROOT)["canary_reason"] == "cap_not_reached"
    approved = _review(q, task_id="review-canary-approved", fix_round=1,
                       verdict="approve")
    assert _run(q, approved) is None
    test = auto_enqueue_test(q, approved, pr_state_fn=lambda _: "open")
    assert test is not None
    assert {t.task_id: t.task_type for t in q.list_tasks()}[test] == "test"
    assert q.get_tokenomics_shadow_receipt(ROOT)["canary_applied"] == 0
    assert q.get_tokenomics_shadow_receipt(ROOT)["canary_reason"] == "cap_not_reached"
