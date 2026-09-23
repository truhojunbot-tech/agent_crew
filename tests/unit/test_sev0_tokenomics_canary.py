"""SEV-0 §11 ONE-TASK tokenomics canary — the config flip, and nothing else.

The matched evidence (quota-core sev0/phaseb-80-contract-emitter 869a3cf)
rejected generic review-fix suppression and supports only this: do not
re-dispatch a review for a commit a standing `request_changes` already
describes. These tests hold that line — one task affected, everything else
shadow, and `unset` as the whole rollback.
"""
import json

import pytest

from agent_crew import tokenomics_canary as canary
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


SHA_A = "a" * 40
SHA_B = "b" * 40


def _review(task_id, *, parent, sha, pr=7, branch="feat/x"):
    return TaskRequest(
        task_id=task_id, task_type="review", description="Review PR #7",
        branch=branch,
        context={"prev_task_id": parent, "pr_number": pr, "reviewed_sha": sha},
    )


def _standing(verdict, task_id="review-impl-1-r0"):
    def lookup(**_kw):
        return {"task_id": task_id, "verdict": verdict, "status": "completed",
                "findings_count": 3}
    return lookup


def _nothing_standing(**_kw):
    return None


# ── the pin ────────────────────────────────────────────────────────────────

def test_env_is_read_per_call_so_unset_is_the_rollback(monkeypatch):
    monkeypatch.setenv(canary.CANARY_ENV, "impl-1")
    assert canary.canary_pin() == "impl-1"
    monkeypatch.delenv(canary.CANARY_ENV)
    assert canary.canary_pin() == ""


def test_unset_env_restores_dispatch_even_when_the_condition_holds(monkeypatch):
    monkeypatch.delenv(canary.CANARY_ENV, raising=False)
    decision = canary.evaluate_review_dispatch(
        _review("review-impl-1-r1", parent="impl-1", sha=SHA_A),
        reviewed_sha=SHA_A, standing_lookup=_standing("request_changes"))
    assert decision.applied is False
    assert decision.reason == "condition_holds_canary_unarmed"
    # still measured — the shadow row is the canary's control group
    assert decision.extra["condition_holds"] is True
    assert decision.counterfactual.endswith(SHA_A)


def test_every_other_task_id_stays_shadow(monkeypatch):
    monkeypatch.setenv(canary.CANARY_ENV, "impl-SOMEONE-ELSE")
    decision = canary.evaluate_review_dispatch(
        _review("review-impl-1-r1", parent="impl-1", sha=SHA_A),
        reviewed_sha=SHA_A, standing_lookup=_standing("request_changes"))
    assert decision.applied is False
    assert decision.reason == "condition_holds_not_the_pinned_task"


def test_applied_for_the_pinned_task_when_the_condition_holds(monkeypatch):
    monkeypatch.setenv(canary.CANARY_ENV, "impl-1")
    decision = canary.evaluate_review_dispatch(
        _review("review-impl-1-r1", parent="impl-1", sha=SHA_A),
        reviewed_sha=SHA_A, standing_lookup=_standing("request_changes"))
    assert decision.applied is True
    assert decision.reason == "standing_request_changes_on_identical_sha"
    assert decision.standing_review_task_id == "review-impl-1-r0"


def test_the_review_tasks_own_id_also_arms_exactly_one_task(monkeypatch):
    monkeypatch.setenv(canary.CANARY_ENV, "review-impl-1-r1")
    decision = canary.evaluate_review_dispatch(
        _review("review-impl-1-r1", parent="impl-1", sha=SHA_A),
        reviewed_sha=SHA_A, standing_lookup=_standing("request_changes"))
    assert decision.applied is True


# ── the condition ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("sha", ["", "HEAD", "abc1234", "z" * 40])
def test_an_unusable_sha_can_never_suppress(monkeypatch, sha):
    monkeypatch.setenv(canary.CANARY_ENV, "impl-1")
    decision = canary.evaluate_review_dispatch(
        _review("review-impl-1-r1", parent="impl-1", sha=sha),
        reviewed_sha=sha, standing_lookup=_standing("request_changes"))
    assert decision.applied is False
    assert decision.reason == "no_reviewed_sha"


def test_no_prior_verdict_on_this_sha_dispatches(monkeypatch):
    monkeypatch.setenv(canary.CANARY_ENV, "impl-1")
    decision = canary.evaluate_review_dispatch(
        _review("review-impl-1-r1", parent="impl-1", sha=SHA_A),
        reviewed_sha=SHA_A, standing_lookup=_nothing_standing)
    assert decision.applied is False
    assert decision.reason == "no_prior_verdict_on_this_sha"


def test_a_standing_approve_is_not_a_standing_request_changes(monkeypatch):
    monkeypatch.setenv(canary.CANARY_ENV, "impl-1")
    decision = canary.evaluate_review_dispatch(
        _review("review-impl-1-r1", parent="impl-1", sha=SHA_A),
        reviewed_sha=SHA_A, standing_lookup=_standing("approve"))
    assert decision.applied is False
    assert decision.reason == "standing_verdict_is_approve"


def test_a_lookup_that_raises_dispatches_rather_than_suppressing(monkeypatch):
    monkeypatch.setenv(canary.CANARY_ENV, "impl-1")

    def boom(**_kw):
        raise RuntimeError("db gone")

    decision = canary.evaluate_review_dispatch(
        _review("review-impl-1-r1", parent="impl-1", sha=SHA_A),
        reviewed_sha=SHA_A, standing_lookup=boom)
    assert decision.applied is False
    assert decision.reason == "standing_lookup_failed"


def test_non_review_tasks_are_never_touched(monkeypatch):
    monkeypatch.setenv(canary.CANARY_ENV, "impl-1")
    impl = TaskRequest(task_id="impl-1", task_type="implement", description="x",
                       branch="feat/x", context={"reviewed_sha": SHA_A})
    decision = canary.evaluate_review_dispatch(
        impl, reviewed_sha=SHA_A, standing_lookup=_standing("request_changes"))
    assert decision.applied is False
    assert decision.reason == "not_a_review_task"


# ── the standing-review lookup, against a real queue ───────────────────────

def _finish_review(queue, task_id, *, verdict, pr, sha, branch="feat/x"):
    queue.enqueue(TaskRequest(task_id=task_id, task_type="review", description="r",
                              branch=branch,
                              context={"pr_number": pr, "reviewed_sha": sha}))
    queue.submit_result(task_id, TaskResult(
        task_id=task_id, status="completed", summary="reviewed",
        verdict=verdict, findings=["f1"] if verdict == "request_changes" else [],
        pr_number=pr))


def test_lookup_matches_only_the_identical_pr_and_sha(tmp_db):
    queue = TaskQueue(tmp_db)
    _finish_review(queue, "rev-old", verdict="request_changes", pr=7, sha=SHA_A)

    hit = queue.standing_request_changes_review(pr_number=7, reviewed_sha=SHA_A)
    assert hit["task_id"] == "rev-old" and hit["verdict"] == "request_changes"

    assert queue.standing_request_changes_review(pr_number=7, reviewed_sha=SHA_B) is None
    assert queue.standing_request_changes_review(pr_number=8, reviewed_sha=SHA_A) is None
    assert queue.standing_request_changes_review(
        pr_number=7, reviewed_sha=SHA_A, exclude_task_id="rev-old") is None


def test_a_later_approve_on_the_same_sha_supersedes_the_request_changes(tmp_db):
    queue = TaskQueue(tmp_db)
    _finish_review(queue, "rev-1", verdict="request_changes", pr=7, sha=SHA_A)
    _finish_review(queue, "rev-2", verdict="approve", pr=7, sha=SHA_A)
    assert queue.standing_request_changes_review(
        pr_number=7, reviewed_sha=SHA_A)["verdict"] == "approve"


def test_the_later_verdict_wins_when_the_reviews_complete_out_of_order(tmp_db):
    """Claim order is not verdict order.

    Two reviewers are dispatched on the same commit; the one claimed second
    answers first. Ordering by ``last_activity_at`` (claim time) returned that
    row, so a ``request_changes`` already withdrawn by the later ``approve``
    suppressed the next review. The standing verdict is the newest *verdict*.
    """
    queue = TaskQueue(tmp_db)
    for task_id in ("rev-a", "rev-b"):
        queue.enqueue(TaskRequest(
            task_id=task_id, task_type="review", description="r", branch="feat/x",
            context={"pr_number": 7, "reviewed_sha": SHA_A}))

    # claimed A first, then B — so B holds the later `last_activity_at`
    assert queue.dequeue(role="reviewer").task_id == "rev-a"
    assert queue.dequeue(role="reviewer").task_id == "rev-b"

    # ...but B answers first, and A's `approve` is the later verdict
    queue.submit_result("rev-b", TaskResult(
        task_id="rev-b", status="completed", summary="r", verdict="request_changes",
        findings=["f1"], pr_number=7))
    queue.submit_result("rev-a", TaskResult(
        task_id="rev-a", status="completed", summary="r", verdict="approve",
        findings=[], pr_number=7))

    hit = queue.standing_request_changes_review(pr_number=7, reviewed_sha=SHA_A)
    assert hit["task_id"] == "rev-a", (
        "ordered by claim time, not verdict time — the superseded "
        "request_changes would suppress the next review")
    assert hit["verdict"] == "approve"


def test_a_legacy_row_without_a_verdict_timestamp_still_orders(tmp_db):
    """Rows predating `status_changed_at` store its DEFAULT 0. They must fall
    back to `last_activity_at` rather than sorting behind everything."""
    import sqlite3

    queue = TaskQueue(tmp_db)
    _finish_review(queue, "rev-legacy", verdict="request_changes", pr=7, sha=SHA_A)
    conn = sqlite3.connect(tmp_db)
    conn.execute("UPDATE tasks SET status_changed_at = 0, last_activity_at = ? "
                 "WHERE task_id = 'rev-legacy'", (1_000.0,))
    conn.commit()
    conn.close()

    hit = queue.standing_request_changes_review(pr_number=7, reviewed_sha=SHA_A)
    assert hit["task_id"] == "rev-legacy" and hit["verdict"] == "request_changes"


# ── the receipt row ────────────────────────────────────────────────────────

def test_applied_receipt_carries_every_field_the_canary_is_judged_on(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(_review("review-impl-1-r1", parent="impl-1", sha=SHA_A))
    decision = canary.evaluate_review_dispatch(
        _review("review-impl-1-r1", parent="impl-1", sha=SHA_A),
        reviewed_sha=SHA_A, standing_lookup=_standing("request_changes"),
        pin="impl-1")
    queue.record_tokenomics_canary_receipt(
        "review-impl-1-r1", decision_source=decision.decision_source,
        recommendation=decision.recommendation(), applied=decision.applied,
        counterfactual=decision.counterfactual, reason=decision.reason,
        cea_receipt_id="9bb6acd2-1c46-4d5e-ac66-d62e656af035")

    row = queue.get_tokenomics_shadow_receipt("review-impl-1-r1")
    assert row["canary_decision_source"] == "quota_core_contract"
    assert row["canary_applied"] == 1
    assert row["canary_reason"] == "standing_request_changes_on_identical_sha"
    assert row["canary_counterfactual"] == (
        f"review would have been dispatched on unchanged sha {SHA_A}")
    assert row["canary_cea_receipt_id"] == "9bb6acd2-1c46-4d5e-ac66-d62e656af035"
    rec = json.loads(row["canary_recommendation_json"])
    assert rec["kind"] == "suppress_identical_sha_rereview"
    assert rec["shadow_decision_source"] == "quota_core_contract"
    assert rec["applied"] is True
    assert rec["reviewed_sha"] == SHA_A and rec["target"] == "pr:7"


def test_a_shadow_receipt_records_applied_false_with_its_reason(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(_review("review-impl-9-r1", parent="impl-9", sha=SHA_A))
    decision = canary.evaluate_review_dispatch(
        _review("review-impl-9-r1", parent="impl-9", sha=SHA_A),
        reviewed_sha=SHA_A, standing_lookup=_nothing_standing, pin="impl-1")
    queue.record_tokenomics_canary_receipt(
        "review-impl-9-r1", decision_source=decision.decision_source,
        recommendation=decision.recommendation(), applied=decision.applied,
        counterfactual=decision.counterfactual, reason=decision.reason)
    row = queue.get_tokenomics_shadow_receipt("review-impl-9-r1")
    assert row["canary_applied"] == 0
    assert row["canary_reason"] == "no_prior_verdict_on_this_sha"
    assert row["canary_counterfactual"] is None


def test_the_canary_never_overwrites_the_342_shadow_columns(tmp_db):
    """`_refresh_shadow_after_commit` owns `shadow_*`; the canary owns `canary_*`."""
    queue = TaskQueue(tmp_db)
    queue.enqueue(_review("review-impl-2-r1", parent="impl-2", sha=SHA_A))
    before = queue.get_tokenomics_shadow_receipt("review-impl-2-r1")
    queue.record_tokenomics_canary_receipt(
        "review-impl-2-r1", decision_source="quota_core_contract",
        recommendation={"kind": canary.RECOMMENDATION_KIND}, applied=True,
        counterfactual="c", reason="r")
    after = queue.get_tokenomics_shadow_receipt("review-impl-2-r1")
    for column in ("decision_source", "policy_version", "recommendation_json",
                   "shadow_decision_source", "shadow_recommendation_json",
                   "shadow_reason", "actual_execution_json"):
        assert after[column] == before[column]


def test_a_suppressed_review_is_terminal_and_readable(tmp_db):
    """`blocked` must resolve for a waiting client — it will never be POSTed."""
    queue = TaskQueue(tmp_db)
    queue.enqueue(_review("review-impl-3-r1", parent="impl-3", sha=SHA_A))
    queue.submit_result("review-impl-3-r1", TaskResult(
        task_id="review-impl-3-r1", status="blocked",
        summary=canary.SUPPRESSED_REASON))
    result = queue.get_result("review-impl-3-r1")
    assert result is not None
    assert result.status == "blocked"
    assert result.summary == canary.SUPPRESSED_REASON


# ── the dispatch path ──────────────────────────────────────────────────────

class _RecordingPush:
    def __init__(self):
        self.calls = []

    def __call__(self, pane_id, text):
        self.calls.append((pane_id, text))


def _seed_standing_request_changes(tmp_db, *, pr=7, sha=SHA_A):
    """A completed request_changes on (pr, sha), written without a cascade."""
    queue = TaskQueue(tmp_db)
    _finish_review(queue, "rev-standing", verdict="request_changes", pr=pr, sha=sha)
    return queue


def _post_rereview(client, *, pr=7, sha=SHA_A):
    return client.post("/tasks", json={
        "task_id": "review-impl-77-r1", "task_type": "review",
        "description": f"Review PR #{pr} for task impl-77.", "branch": "feat/x",
        "priority": 3,
        "context": {"prev_task_id": "impl-77", "pr_number": pr, "reviewed_sha": sha},
        "project": "",
    })


def test_armed_canary_does_not_push_the_identical_sha_rereview(monkeypatch, tmp_db):
    from fastapi.testclient import TestClient
    from agent_crew.server import create_app

    queue = _seed_standing_request_changes(tmp_db)
    monkeypatch.setenv(canary.CANARY_ENV, "impl-77")
    push = _RecordingPush()
    app = create_app(db_path=tmp_db, pane_map={"reviewer": "%200"}, port=9999,
                     push_fn=push)
    with TestClient(app) as client:
        assert _post_rereview(client).status_code == 201

    assert push.calls == [], "the reviewer was spent on an unchanged sha"
    result = queue.get_result("review-impl-77-r1")
    assert result is not None and result.status == "blocked"
    assert result.summary == canary.SUPPRESSED_REASON

    row = queue.get_tokenomics_shadow_receipt("review-impl-77-r1")
    assert row["canary_applied"] == 1
    assert row["canary_decision_source"] == "quota_core_contract"
    assert row["canary_counterfactual"].endswith(SHA_A)
    assert json.loads(row["canary_recommendation_json"])["kind"] == (
        "suppress_identical_sha_rereview")


def test_unset_env_pushes_the_same_rereview_and_records_shadow(monkeypatch, tmp_db):
    from fastapi.testclient import TestClient
    from agent_crew.server import create_app

    queue = _seed_standing_request_changes(tmp_db)
    monkeypatch.delenv(canary.CANARY_ENV, raising=False)
    push = _RecordingPush()
    app = create_app(db_path=tmp_db, pane_map={"reviewer": "%200"}, port=9999,
                     push_fn=push)
    with TestClient(app) as client:
        assert _post_rereview(client).status_code == 201

    assert len(push.calls) == 1 and push.calls[0][0] == "%200"
    row = queue.get_tokenomics_shadow_receipt("review-impl-77-r1")
    assert row["canary_applied"] == 0
    assert row["canary_reason"] == "condition_holds_canary_unarmed"


def test_a_pin_on_another_task_leaves_this_rereview_alone(monkeypatch, tmp_db):
    from fastapi.testclient import TestClient
    from agent_crew.server import create_app

    queue = _seed_standing_request_changes(tmp_db)
    monkeypatch.setenv(canary.CANARY_ENV, "impl-SOMEONE-ELSE")
    push = _RecordingPush()
    app = create_app(db_path=tmp_db, pane_map={"reviewer": "%200"}, port=9999,
                     push_fn=push)
    with TestClient(app) as client:
        assert _post_rereview(client).status_code == 201

    assert len(push.calls) == 1
    row = queue.get_tokenomics_shadow_receipt("review-impl-77-r1")
    assert row["canary_applied"] == 0
    assert row["canary_reason"] == "condition_holds_not_the_pinned_task"


def test_armed_canary_still_pushes_when_the_sha_moved(monkeypatch, tmp_db):
    from fastapi.testclient import TestClient
    from agent_crew.server import create_app

    queue = _seed_standing_request_changes(tmp_db, sha=SHA_A)
    monkeypatch.setenv(canary.CANARY_ENV, "impl-77")
    push = _RecordingPush()
    app = create_app(db_path=tmp_db, pane_map={"reviewer": "%200"}, port=9999,
                     push_fn=push)
    with TestClient(app) as client:
        assert _post_rereview(client, sha=SHA_B).status_code == 201

    assert len(push.calls) == 1, "a real new commit must still be reviewed"
    row = queue.get_tokenomics_shadow_receipt("review-impl-77-r1")
    assert row["canary_applied"] == 0
    assert row["canary_reason"] == "no_prior_verdict_on_this_sha"
