"""#914 — a refused auto-fix must not end the lineage silently.

Observed 2026-10-28 on 8107: four request_changes reviews of implement tasks
that ran on the default branch ``master`` hit the #457 guard. The guard only
logged a WARNING, so no fix, no notice and no needs_human — the crew sat idle
for 11 h. The guard stays; its refusal now records a terminal reason on the
review task and goes through the coordinator notice sink (#864).
"""

import uuid

import pytest

from agent_crew.pipeline import auto_enqueue_fix, auto_enqueue_review, fix_task_id
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue

FINDING = "HIGH trader.py:12 - order size unchecked"


def _open(pr):
    return "open"


@pytest.fixture
def q(tmp_db, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)
    monkeypatch.delenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", raising=False)
    return TaskQueue(tmp_db)


def _review(q, branch):
    review_id = f"review-{uuid.uuid4().hex[:8]}"
    q.enqueue(TaskRequest(task_id=review_id, task_type="review",
                          description="review", branch=branch, context={}))
    q.submit_result(review_id, TaskResult(
        task_id=review_id, status="completed", summary="request_changes",
        verdict="request_changes", findings=[FINDING]))
    return review_id


@pytest.mark.parametrize("branch", ["master", "main"])
def test_default_branch_refusal_records_reason_and_notifies_once(q, branch, monkeypatch):
    if branch == "master":  # 8107's repository default
        monkeypatch.setenv("AGENT_CREW_MAIN_BRANCH", "master")
    sent = []
    review_id = _review(q, branch)

    for _ in range(2):  # a replayed result POST must not notify twice
        assert auto_enqueue_fix(q, review_id, pr_state_fn=_open,
                                coordinator_notify_fn=lambda m: sent.append(m) or True) is None

    assert [t for t in q.list_tasks() if t.task_type == "implement"] == []
    ctx = q.get_task(review_id).context
    assert ctx["lineage_terminal_reason"] == "successor_refused: default_branch"
    assert len(sent) == 1
    assert f"task={review_id}" in sent[0]
    assert "kind=successor_refused" in sent[0]
    assert "default_branch" in sent[0]


def test_feature_branch_fix_sends_no_refusal_notice(q):
    sent = []
    review_id = _review(q, "agent/codex/914-x")

    assert auto_enqueue_fix(q, review_id, pr_state_fn=_open,
                            coordinator_notify_fn=lambda m: sent.append(m) or True
                            ) == fix_task_id(review_id, 1)
    assert sent == []
    assert "lineage_terminal_reason" not in q.get_task(review_id).context


def _notify(sent):
    return lambda m: sent.append(m) or True


def _exhausted_review(q, context):
    review_id = f"review-{uuid.uuid4().hex[:8]}"
    q.enqueue(TaskRequest(task_id=review_id, task_type="review", description="review",
                          branch="agent/codex/914-x", context=context))
    q.submit_result(review_id, TaskResult(
        task_id=review_id, status="completed", summary="request_changes",
        verdict="request_changes", findings=[FINDING]))
    return review_id


def test_missing_branch_and_pr_review_refusal_records_reason_and_notifies_once(q):
    sent = []
    impl_id = f"impl-{uuid.uuid4().hex[:8]}"
    q.enqueue(TaskRequest(task_id=impl_id, task_type="implement",
                          description="implement", branch="", context={}))
    q.submit_result(impl_id, TaskResult(task_id=impl_id, status="completed",
                                        summary="done"))

    for _ in range(2):  # replayed result POST
        assert auto_enqueue_review(q, impl_id, coordinator_notify_fn=_notify(sent)) is None

    assert [t for t in q.list_tasks() if t.task_type == "review"] == []
    ctx = q.get_task(impl_id).context
    assert ctx["lineage_terminal_reason"] == "successor_refused: no_branch_no_pr"
    assert len(sent) == 1
    assert f"task={impl_id}" in sent[0] and "kind=successor_refused" in sent[0]
    assert "no_branch_no_pr" in sent[0]


def test_review_with_branch_sends_no_refusal_notice(q):
    sent = []
    impl_id = f"impl-{uuid.uuid4().hex[:8]}"
    q.enqueue(TaskRequest(task_id=impl_id, task_type="implement",
                          description="implement", branch="agent/codex/914-x", context={}))
    q.submit_result(impl_id, TaskResult(task_id=impl_id, status="completed",
                                        summary="done"))

    assert auto_enqueue_review(q, impl_id, pr_state_fn=_open,
                               coordinator_notify_fn=_notify(sent)) is not None
    assert sent == []
    assert "lineage_terminal_reason" not in q.get_task(impl_id).context


@pytest.mark.parametrize("replaying", [False, True])
def test_exhausted_fix_rounds_records_reason_and_notifies_once(q, replaying):
    sent = []
    review_id = _exhausted_review(q, {"fix_round": 3})

    for _ in range(2):
        assert auto_enqueue_fix(q, review_id, pr_state_fn=_open,
                                suppress_side_effects=replaying,
                                coordinator_notify_fn=_notify(sent)) is None

    assert [t for t in q.list_tasks() if t.task_type == "implement"] == []
    ctx = q.get_task(review_id).context
    assert ctx["lineage_terminal_reason"] == "successor_refused: fix_rounds_exhausted"
    assert len(sent) == 1
    assert "kind=successor_refused" in sent[0] and "round=4 max=3" in sent[0]


def test_exhausted_rounds_on_terminal_pr_stays_silent(q):
    sent = []
    review_id = _exhausted_review(q, {"fix_round": 3, "pr_number": 923})

    assert auto_enqueue_fix(q, review_id, pr_state_fn=lambda pr: "merged",
                            coordinator_notify_fn=_notify(sent)) is None
    assert sent == []
    assert "lineage_terminal_reason" not in q.get_task(review_id).context


def test_duplicate_fix_delivery_is_not_a_refusal(q):
    sent = []
    review_id = _review(q, "agent/codex/914-x")

    assert auto_enqueue_fix(q, review_id, pr_state_fn=_open,
                            coordinator_notify_fn=_notify(sent)) == fix_task_id(review_id, 1)
    # Replayed result: the fix already exists, so nothing new and nothing refused.
    assert auto_enqueue_fix(q, review_id, pr_state_fn=_open,
                            coordinator_notify_fn=_notify(sent)) is None
    assert sent == []
    assert "lineage_terminal_reason" not in q.get_task(review_id).context


def test_exhausted_failed_test_leaves_notice_to_result_handler(q):
    sent = []
    test_id = f"test-{uuid.uuid4().hex[:8]}"
    q.enqueue(TaskRequest(task_id=test_id, task_type="test", description="test",
                          branch="agent/codex/914-x", context={"fix_round": 3}))
    q.submit_result(test_id, TaskResult(task_id=test_id, status="failed",
                                        summary="tests failed", findings=[FINDING]))

    assert auto_enqueue_fix(q, test_id, pr_state_fn=_open,
                            coordinator_notify_fn=_notify(sent)) is None
    assert sent == []  # server sends test_failed_no_successor instead
