"""#914 — a refused auto-fix must not end the lineage silently.

Observed 2026-10-28 on 8107: four request_changes reviews of implement tasks
that ran on the default branch ``master`` hit the #457 guard. The guard only
logged a WARNING, so no fix, no notice and no needs_human — the crew sat idle
for 11 h. The guard stays; its refusal now records a terminal reason on the
review task and goes through the coordinator notice sink (#864).
"""

import uuid

import pytest

from agent_crew.pipeline import auto_enqueue_fix, fix_task_id
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
