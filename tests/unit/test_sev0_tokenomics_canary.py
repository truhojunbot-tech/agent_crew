"""SEV-0 §11 ONE-TASK tokenomics canary — the config flip, and nothing else.

The matched evidence (quota-core sev0/phaseb-80-contract-emitter 869a3cf)
rejected generic review-fix suppression and supports only this: do not
re-dispatch a review for a commit a standing `request_changes` already
describes. These tests hold that line — one task affected, everything else
shadow, and `unset` as the whole rollback.
"""
import contextlib
import json

import pytest

from agent_crew import tokenomics_canary as canary
from agent_crew.loop import handle_review_result, next_review_action
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


def _standing(verdict, task_id="review-impl-1-r0", findings=None):
    _findings = ["code_quality: name it", {"layer": "test_quality", "issue": "no edge case"}] \
        if findings is None else findings

    def lookup(**_kw):
        return {"task_id": task_id, "verdict": verdict, "status": "completed",
                "findings": list(_findings), "findings_count": len(_findings)}
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


def test_the_review_tasks_own_id_does_not_expand_the_owner_pinned_lineage(monkeypatch):
    """§11 arms the parent implement task, never a review task id."""
    monkeypatch.setenv(canary.CANARY_ENV, "review-impl-1-r1")
    decision = canary.evaluate_review_dispatch(
        _review("review-impl-1-r1", parent="impl-1", sha=SHA_A),
        reviewed_sha=SHA_A, standing_lookup=_standing("request_changes"))
    assert decision.applied is False
    assert decision.reason == "condition_holds_not_the_pinned_task"


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


def test_completion_shadow_refresh_never_erases_the_canary_measurement(tmp_db):
    """Completion refresh owns ``shadow_*`` only, never the canary evidence."""
    queue = TaskQueue(tmp_db)
    task = _review("review-impl-canary-r1", parent="impl-canary", sha=SHA_A)
    queue.enqueue(task)
    queue.record_tokenomics_canary_receipt(
        task.task_id, decision_source="quota_core_contract",
        recommendation={"kind": canary.RECOMMENDATION_KIND, "applied": True},
        applied=True, counterfactual="would dispatch", reason="standing")
    queue.submit_result(task.task_id, TaskResult(
        task_id=task.task_id, status="blocked", summary=canary.SUPPRESSED_REASON))

    row = queue.get_tokenomics_shadow_receipt(task.task_id)
    assert row["canary_applied"] == 1
    assert json.loads(row["canary_recommendation_json"])["kind"] == canary.RECOMMENDATION_KIND
    assert row["canary_reason"] == "standing"


def test_a_suppressed_review_takes_the_ordinary_request_changes_path():
    """§11 item B: suppression reuses a verdict — it is not a failed review.

    `review_failed` sent this through `next_review_action`: retry, retry, give
    up — and each retry's `enqueue_review` minted a fresh review at the same
    head, which the canary suppressed again.
    """
    suppressed = TaskResult(
        task_id="review-impl-3-r1", status="completed",
        summary=canary.SUPPRESSED_REASON, verdict="request_changes",
        findings=[{"layer": "code_quality", "issue": "name it",
                   canary.REUSED_FROM_KEY: "rev-standing"}])
    assert handle_review_result(suppressed, iteration=1, max_iter=5,
                                no_tester=True) == "request_changes"
    assert next_review_action("request_changes", attempts=0) == ""


def test_a_legacy_blocked_suppression_also_takes_the_fix_path():
    """Rows written before item B carry no verdict; they must not be retried."""
    legacy = TaskResult(
        task_id="review-impl-3-r1", status="blocked",
        summary=canary.SUPPRESSED_REASON)
    assert handle_review_result(legacy, iteration=1, max_iter=5,
                                no_tester=True) == "request_changes"


def test_a_real_review_failure_is_still_retried():
    """The suppression exemption is keyed on the reason, not on 'terminal'."""
    crashed = TaskResult(task_id="review-impl-3-r1", status="failed",
                         summary="exit_1")
    assert handle_review_result(crashed, iteration=1, max_iter=5,
                                no_tester=True) == "review_failed"
    assert next_review_action("review_failed", attempts=0) == "retry"


def test_neither_cli_review_loop_special_cases_a_suppression_any_more():
    """Both loops must reach `build_feedback` exactly as after a real review."""
    import inspect

    from agent_crew import cli

    assert "review_suppressed" not in inspect.getsource(cli)


def test_reused_findings_are_tagged_with_the_review_that_made_them():
    reused = canary.reuse_findings(
        ["code_quality: name it", {"layer": "test_quality", "issue": "edge"}],
        "rev-standing")
    assert all(f[canary.REUSED_FROM_KEY] == "rev-standing" for f in reused)
    assert reused[0]["issue"] == "code_quality: name it"
    assert reused[1]["layer"] == "test_quality"

    # and the wrapped string still renders under its own layer
    from agent_crew.loop import build_feedback
    text = build_feedback(TaskResult(task_id="t", status="completed",
                                     summary="reviewed", findings=reused))
    assert "[code_quality] name it (standing finding reused from rev-standing)" in text
    assert "[test_quality] edge (standing finding reused from rev-standing)" in text


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
    assert result is not None and result.status == "completed", (
        "a suppression is a reused verdict, not a dead review")
    assert result.summary == canary.SUPPRESSED_REASON
    assert result.verdict == "request_changes"
    assert [f[canary.REUSED_FROM_KEY] for f in result.findings] == ["rev-standing"]

    row = queue.get_tokenomics_shadow_receipt("review-impl-77-r1")
    assert row["canary_applied"] == 1
    assert row["canary_decision_source"] == "quota_core_contract"
    assert row["canary_counterfactual"].endswith(SHA_A)
    assert json.loads(row["canary_recommendation_json"])["kind"] == (
        "suppress_identical_sha_rereview")
    assert row["shadow_resolved_at"] is not None
    assert queue.outbox_get("review-impl-77-r1")["state"] == "applied"


def test_a_suppression_settles_its_authorization_receipt(monkeypatch, tmp_db):
    """A terminal task must not leave an open authorization behind.

    `submit_result` ends a receipt at CONSUMED, which §3 reaches only from
    CLAIMED/RUNNING. A suppressed review is decided at dispatch, so nothing
    ever claimed it: that transition is refused and the receipt would stay
    QUEUED forever next to an already-terminal task.
    """
    from fastapi.testclient import TestClient
    from agent_crew.cea import store as cea_store
    from agent_crew.server import create_app

    queue = _seed_standing_request_changes(tmp_db)
    monkeypatch.setenv(canary.CANARY_ENV, "impl-77")
    app = create_app(db_path=tmp_db, pane_map={"reviewer": "%200"}, port=9999,
                     push_fn=_RecordingPush())
    with TestClient(app) as client:
        assert _post_rereview(client).status_code == 201

    receipt_id = queue.task_receipt_id("review-impl-77-r1")
    assert receipt_id, "the review was admitted without a receipt — nothing to settle"
    conn = queue._connect()
    try:
        state = (cea_store.current_receipt(conn, receipt_id) or {}).get("state")
    finally:
        conn.close()
    assert state == "REVOKED", state


def test_settling_an_unclaimed_receipt_revokes_it_once(tmp_db):
    """The transition the suppression relies on, isolated.

    REVOKED, not CONSUMED: the reviewer invocation this receipt authorized was
    withdrawn, never spent. And terminal is terminal — a second call must not
    rewrite how the receipt ended.
    """
    from agent_crew.cea import store as cea_store

    queue = TaskQueue(tmp_db)
    queue.enqueue(_review("review-impl-8-r1", parent="impl-8", sha=SHA_A))
    receipt_id = queue.task_receipt_id("review-impl-8-r1")
    assert receipt_id
    conn = queue._connect()
    try:
        assert (cea_store.current_receipt(conn, receipt_id) or {}).get("state") \
            not in cea_store.TERMINAL_STATES
    finally:
        conn.close()

    assert queue.settle_unused_task_receipt(
        "review-impl-8-r1", note="canary") == "REVOKED"
    assert queue.settle_unused_task_receipt(
        "review-impl-8-r1", note="again") == "REVOKED"


def test_a_suppression_drives_the_fix_cascade_like_a_real_review(monkeypatch, tmp_db):
    """The point of item B: the lineage continues to the fix step.

    The cascade lives on the HTTP result endpoint, and a suppression's result
    never passes through it — so unless the suppression drives it, the reused
    verdict is recorded and nothing acts on it. What the cascade then decides
    (a terminal PR, a moved head) is its own business and covered by #250/#253;
    what matters here is that it is asked at all.
    """
    from fastapi.testclient import TestClient

    from agent_crew import server as sv

    _seed_standing_request_changes(tmp_db)
    monkeypatch.setenv(canary.CANARY_ENV, "impl-77")
    asked = []
    monkeypatch.setattr(sv, "_pipeline_auto_enqueue_fix",
                        lambda queue, review_task_id, **kw: asked.append(review_task_id))
    app = sv.create_app(db_path=tmp_db, pane_map={"reviewer": "%200"}, port=9999,
                        push_fn=_RecordingPush())
    with TestClient(app) as client:
        assert _post_rereview(client).status_code == 201

    assert asked == ["review-impl-77-r1"]


def test_the_pipeline_cascade_creates_the_fix_from_a_suppressed_review(tmp_db):
    """`auto_enqueue_fix` reads a suppression as the request_changes it reuses.

    It drops any review whose status is not `completed`, so the old `blocked`
    shape ended the lineage here silently.
    """
    from agent_crew.pipeline import auto_enqueue_fix

    queue = _seed_standing_request_changes(tmp_db)
    queue.enqueue(_review("review-impl-77-r1", parent="impl-77", sha=SHA_A))
    queue.submit_result("review-impl-77-r1", TaskResult(
        task_id="review-impl-77-r1", status="completed",
        summary=canary.SUPPRESSED_REASON, verdict="request_changes",
        findings=canary.reuse_findings(["code_quality: name it"], "rev-standing"),
        pr_number=7))

    fix_id = auto_enqueue_fix(
        queue, "review-impl-77-r1",
        pr_state_fn=lambda *a, **k: "open",
        head_sha_fn=lambda *a, **k: SHA_A,
        suppress_side_effects=True)

    assert fix_id, "the reused verdict produced no fix round"
    fix = {t.task_id: t for t in queue.list_tasks()}[fix_id]
    assert fix.task_type == "implement"
    assert (fix.context or {}).get("fix_round") == 1
    reused = (fix.context or {}).get("review_findings") or []
    assert reused and all(f.get(canary.REUSED_FROM_KEY) == "rev-standing"
                          for f in reused)


def test_a_suppression_mints_no_second_review_at_the_same_head(tmp_db):
    """`enqueue_review` must REUSE the suppressed task, not replace it.

    `blocked` is in `loop._DEAD_REVIEW_STATUSES`, so the old shape made this
    helper skip the suppressed review and mint a fresh `review-<uuid8>` at the
    identical head — suppressed again, one more canary receipt each round.
    """
    from agent_crew.loop import enqueue_review

    queue = TaskQueue(tmp_db)
    queue.enqueue(_review("review-impl-9-r1", parent="impl-9", sha=SHA_A))
    queue.submit_result("review-impl-9-r1", TaskResult(
        task_id="review-impl-9-r1", status="completed",
        summary=canary.SUPPRESSED_REASON, verdict="request_changes",
        findings=canary.reuse_findings(["code_quality: name it"], "rev-standing")))

    assert enqueue_review(queue, "Review PR #7", "feat/x",
                          prev_task_id="impl-9") == "review-impl-9-r1"
    assert [t.task_id for t in queue.list_tasks() if t.task_type == "review"] == (
        ["review-impl-9-r1"])


def test_exactly_one_applied_canary_receipt_per_suppression(monkeypatch, tmp_db):
    """One suppression, one measurement row — `applied` written once."""
    import sqlite3

    from fastapi.testclient import TestClient
    from agent_crew.server import create_app

    _seed_standing_request_changes(tmp_db)
    monkeypatch.setenv(canary.CANARY_ENV, "impl-77")
    app = create_app(db_path=tmp_db, pane_map={"reviewer": "%200"}, port=9999,
                     push_fn=_RecordingPush())
    with TestClient(app) as client:
        assert _post_rereview(client).status_code == 201

    conn = sqlite3.connect(tmp_db)
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM tokenomics_shadow_receipts "
            "WHERE canary_applied = 1").fetchone()[0]
    finally:
        conn.close()
    assert rows == 1, rows


def test_failed_canary_promotion_rolls_back_suppression_and_dispatches(monkeypatch, tmp_db):
    """Promotion failure leaves no terminal verdict or spent receipt behind."""
    from fastapi.testclient import TestClient
    from agent_crew.server import create_app

    _seed_standing_request_changes(tmp_db)
    monkeypatch.setenv(canary.CANARY_ENV, "impl-77")
    push = _RecordingPush()
    monkeypatch.setattr(
        TaskQueue, "_promote_tokenomics_canary_receipt_in_txn",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("promotion failed")),
    )
    app = create_app(db_path=tmp_db, pane_map={"reviewer": "%200"}, port=9999,
                     push_fn=push)
    with TestClient(app) as client:
        assert _post_rereview(client).status_code == 201

    assert len(push.calls) == 1
    queue = TaskQueue(tmp_db)
    assert queue.get_result("review-impl-77-r1") is None
    row = queue.get_tokenomics_shadow_receipt("review-impl-77-r1")
    assert row["canary_applied"] == 0
    assert row["canary_reason"] == "suppression_transaction_failed"
    from agent_crew.cea import store as cea_store
    receipt_id = queue.task_receipt_id("review-impl-77-r1")
    conn = queue._connect()
    try:
        assert (cea_store.current_receipt(conn, receipt_id) or {}).get("state") != "CONSUMED"
    finally:
        conn.close()


def test_post_commit_side_effect_failure_cannot_unsuppress(monkeypatch, tmp_db):
    from fastapi.testclient import TestClient
    from agent_crew import server as sv

    _seed_standing_request_changes(tmp_db)
    monkeypatch.setenv(canary.CANARY_ENV, "impl-77")
    push = _RecordingPush()
    monkeypatch.setattr(sv, "record_context_event",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("jsonl gone")))
    app = sv.create_app(db_path=tmp_db, pane_map={"reviewer": "%200"}, port=9999,
                        push_fn=push)
    with TestClient(app) as client:
        assert _post_rereview(client).status_code == 201

    assert push.calls == []
    row = TaskQueue(tmp_db).get_tokenomics_shadow_receipt("review-impl-77-r1")
    assert row["canary_applied"] == 1


def test_shadow_recommendation_is_byte_identical_to_the_decision(monkeypatch, tmp_db):
    from fastapi.testclient import TestClient
    from agent_crew.server import create_app

    _seed_standing_request_changes(tmp_db)
    monkeypatch.delenv(canary.CANARY_ENV, raising=False)
    task = _review("review-impl-77-r1", parent="impl-77", sha=SHA_A)
    expected = json.dumps(canary.evaluate_review_dispatch(
        task, reviewed_sha=SHA_A,
        standing_lookup=TaskQueue(tmp_db).standing_request_changes_review,
    ).recommendation())
    app = create_app(db_path=tmp_db, pane_map={"reviewer": "%200"}, port=9999,
                     push_fn=_RecordingPush())
    with TestClient(app) as client:
        assert _post_rereview(client).status_code == 201

    row = TaskQueue(tmp_db).get_tokenomics_shadow_receipt("review-impl-77-r1")
    assert row["canary_recommendation_json"] == expected


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


# ── C1: the completion-time refresh leaves canary_* alone ──────────────────

_CANARY_COLUMNS = (
    "canary_decision_source", "canary_recommendation_json", "canary_applied",
    "canary_counterfactual", "canary_reason", "canary_cea_receipt_id",
    "canary_resolved_at",
)


def test_refresh_shadow_after_commit_never_touches_a_canary_column(tmp_db):
    """The direction that motivated separate columns: the canary writes at
    dispatch, then the review completes and `_refresh_shadow_after_commit`
    rewrites the `shadow_*` set. Had the canary shared those columns, its one
    measurement would be erased microseconds after it was taken."""
    queue = TaskQueue(tmp_db)
    queue.enqueue(_review("review-impl-5-r1", parent="impl-5", sha=SHA_A))
    decision = canary.evaluate_review_dispatch(
        _review("review-impl-5-r1", parent="impl-5", sha=SHA_A),
        reviewed_sha=SHA_A, standing_lookup=_standing("request_changes"),
        pin="impl-5")
    queue.record_tokenomics_canary_receipt(
        "review-impl-5-r1", decision_source=decision.decision_source,
        recommendation=decision.recommendation(), applied=decision.applied,
        counterfactual=decision.counterfactual, reason=decision.reason,
        cea_receipt_id="cea-receipt-5")
    before = queue.get_tokenomics_shadow_receipt("review-impl-5-r1")
    assert before["canary_applied"] == 1 and before["shadow_resolved_at"] is None

    queue.submit_result("review-impl-5-r1", TaskResult(
        task_id="review-impl-5-r1", status="completed", summary="reviewed",
        verdict="request_changes", findings=["f1"], pr_number=7))
    # and once more directly, so a refresh that runs twice is covered too
    queue._refresh_shadow_after_commit("review-impl-5-r1", "completed")

    after = queue.get_tokenomics_shadow_receipt("review-impl-5-r1")
    assert after["shadow_resolved_at"] is not None, (
        "the refresh never ran — this test would prove nothing")
    for column in _CANARY_COLUMNS:
        assert after[column] == before[column], column
        assert type(after[column]) is type(before[column]), column


# ── C2: the `_dispatch_task` copy of the gate ──────────────────────────────

class _RecordingExitStack(contextlib.ExitStack):
    instances = []

    def __init__(self):
        super().__init__()
        self.closed = False
        _RecordingExitStack.instances.append(self)

    def close(self):
        self.closed = True
        super().close()


def _dispatch_review(tmp_path, monkeypatch, *, pin):
    """Drive `app.state.dispatch_task` — the dispatcher path, not `_try_push_next`."""
    import asyncio
    import types

    from fastapi.testclient import TestClient

    from agent_crew import server as sv

    spawned = []

    async def _fake_exec(*cmd, **kwargs):
        spawned.append(list(cmd))

        class _P:
            returncode, pid = 0, 1

            async def wait(self):
                return 0

            async def communicate(self, *a, **k):
                return b"", b""
        return _P()

    wt = tmp_path / "codex-wt"
    wt.mkdir()
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"port": 9999, "worktrees": {"codex": str(wt)}}))
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_BASE", str(tmp_path / "lockbase"))
    monkeypatch.delenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", raising=False)
    monkeypatch.setattr(sv, "_WORKTREE_SYNC_DISABLED", False)
    monkeypatch.setattr(sv, "_prepare_worktree_for_task", lambda *a, **k: SHA_A)
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec", _fake_exec)
    if pin is None:
        monkeypatch.delenv(canary.CANARY_ENV, raising=False)
    else:
        monkeypatch.setenv(canary.CANARY_ENV, pin)

    _RecordingExitStack.instances = []
    proxy = types.SimpleNamespace(
        **{k: getattr(contextlib, k) for k in dir(contextlib) if not k.startswith("__")})
    proxy.ExitStack = _RecordingExitStack
    monkeypatch.setattr(sv, "contextlib", proxy)

    db = str(tmp_path / "tasks.db")
    _seed_standing_request_changes(db)
    app = sv.create_app(db_path=db, pane_map={}, port=9999, state_path=str(state),
                        project="demo", watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        queue = TaskQueue(db)
        queue.enqueue(TaskRequest(
            task_id="review-impl-77-r1", task_type="review",
            description="Review PR #7 for task impl-77.", branch="feat/x",
            context={"prev_task_id": "impl-77", "pr_number": 7, "reviewed_sha": SHA_A}))
        task = queue.dequeue(role="reviewer")
        assert task is not None and task.task_id == "review-impl-77-r1"
        _RecordingExitStack.instances = []
        asyncio.run(app.state.dispatch_task(task, "reviewer"))
        row = {t.task_id: t for t in queue.list_tasks()}["review-impl-77-r1"]
        receipt = queue.get_tokenomics_shadow_receipt("review-impl-77-r1")
        result = queue.get_result("review-impl-77-r1")
    return spawned, row, receipt, result, list(_RecordingExitStack.instances), wt


def test_dispatcher_path_suppresses_the_pinned_rereview_and_releases_the_lock(
        tmp_path, monkeypatch):
    spawned, row, receipt, result, stacks, wt = _dispatch_review(
        tmp_path, monkeypatch, pin="impl-77")

    assert spawned == [], "a provider was launched on an unchanged sha"
    assert row.status == "completed", row.status
    assert result is not None and result.summary == canary.SUPPRESSED_REASON
    assert result.verdict == "request_changes"
    assert receipt["canary_applied"] == 1
    assert receipt["canary_reason"] == "standing_request_changes_on_identical_sha"

    # the gate's early return must close `_lock_stack` — no leaked lock
    assert len(stacks) == 1, f"expected one _lock_stack, saw {len(stacks)}"
    assert stacks[0].closed, "_lock_stack left open on the suppression return"
    assert not stacks[0]._exit_callbacks, "a lock is still registered"

    from agent_crew.server import test_stage_lock
    with test_stage_lock(str(wt)) as acquired:
        assert acquired, "the worktree lock is still held after suppression"


def test_dispatcher_path_dispatches_an_unpinned_rereview_normally(
        tmp_path, monkeypatch):
    spawned, row, receipt, result, stacks, wt = _dispatch_review(
        tmp_path, monkeypatch, pin=None)

    assert len(spawned) == 1, "the unpinned review was not dispatched"
    assert (result is None or result.summary != canary.SUPPRESSED_REASON), (
        "an unpinned review must never be recorded as suppressed")
    assert receipt["canary_applied"] == 0
    assert receipt["canary_reason"] == "condition_holds_canary_unarmed"
    assert all(s.closed for s in stacks), "_lock_stack left open after dispatch"
