"""Review admission respects a provider's lineage budget (#967)."""

import pytest

from agent_crew.pipeline import (ReviewRoundCapReached, auto_enqueue_review,
                                 requeue_review_at_head)
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


def _queue(tmp_path, providers=("claude", "claude"), *, cancelled=False):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(task_id="root", task_type="implement", branch="feature", description="fixture",
                              context={"risk_tier": 1, "crew_run_branch": True}))
    for n, provider in enumerate(providers):
        queue.enqueue(TaskRequest(task_id=f"prior-{n}", task_type="review",
                                  branch=f"prior-branch-{n}", description="fixture", context={
                                      "risk_tier": 1, "prev_task_id": "root",
                                      "lineage_root_task_id": "root",
                                      "agent_override": provider}))
    if cancelled:
        queue.cancel("prior-0")
    return queue


def _predecessor(queue, kind="implement"):
    parent = "prior-1"
    if kind == "script_test":
        queue.enqueue(TaskRequest(task_id="test-fail", task_type="test", branch="feature", description="fixture",
                                  context={"risk_tier": 1, "prev_task_id": parent,
                                           "lineage_root_task_id": "root"}))
        parent = "test-fail"
    queue.enqueue(TaskRequest(task_id="fix-current", task_type="implement",
                              branch="feature", description="fixture", context={
                                  "risk_tier": 1, "prev_task_id": parent,
                                  "lineage_root_task_id": "root", "fix_round": 2,
                                  "reviewer_agent": "claude", "crew_run_branch": True,
                                  "pr_number": 7}))
    return "fix-current"


@pytest.mark.parametrize("kind", ["implement", "script_test"])
def test_third_review_is_blocked_and_replay_notifies_once(tmp_path, monkeypatch, kind):
    queue = _queue(tmp_path)
    predecessor = _predecessor(queue, kind)
    monkeypatch.setenv("AGENT_CREW_REVIEW_MAX_ROUNDS_CLAUDE", "2")
    notices = []
    call = lambda: auto_enqueue_review(queue, predecessor, 7,
        pr_state_fn=lambda _: "open", reviewer_provider="claude",
        coordinator_notify_fn=lambda message: notices.append(message) or True)
    outcome = call()
    assert outcome == ReviewRoundCapReached("review_round_cap", "claude", 2, 2, "root")
    assert call() == outcome
    assert queue.get_task("review-fix-current-r2") is None
    context = queue.get_task(predecessor).context
    assert context["review_blocked_reason"] == "review_round_cap"
    assert (context["review_blocked_provider"], context["review_blocked_count"],
            context["review_blocked_cap"]) == ("claude", 2, 2)
    assert len(notices) == 1
    assert "lineage_root=root" in notices[0] and "pr_number=7" in notices[0]


def test_requeue_at_new_head_is_blocked(tmp_path, monkeypatch):
    queue = _queue(tmp_path)
    monkeypatch.setenv("AGENT_CREW_REVIEW_MAX_ROUNDS_CLAUDE", "2")
    notices = []
    call = lambda: requeue_review_at_head(queue, "prior-1", 7, "a" * 40,
        {"risk_tier": 1, "agent_override": "claude"}, project="demo",
        coordinator_notify_fn=lambda message: notices.append(message) or True)
    assert isinstance(call(), ReviewRoundCapReached)
    assert isinstance(call(), ReviewRoundCapReached)
    assert queue.get_task("review-7-aaaaaaaaaaaa") is None
    assert len(notices) == 1
    assert "head_sha=" + "a" * 40 in notices[0]


@pytest.mark.parametrize("providers,cancelled,cap", [
    (("claude", "claude"), True, "2"),
    (("codex", "claude"), False, "2"),
    (("claude", "claude"), False, None),
])
def test_count_excludes_cancelled_and_other_provider_and_unset_is_unlimited(
        tmp_path, monkeypatch, providers, cancelled, cap):
    queue = _queue(tmp_path, providers, cancelled=cancelled)
    predecessor = _predecessor(queue)
    if cap is None:
        monkeypatch.delenv("AGENT_CREW_REVIEW_MAX_ROUNDS_CLAUDE", raising=False)
    else:
        monkeypatch.setenv("AGENT_CREW_REVIEW_MAX_ROUNDS_CLAUDE", cap)
    review_id = auto_enqueue_review(queue, predecessor, 7,
        pr_state_fn=lambda _: "open", reviewer_provider="claude")
    assert review_id == "review-fix-current-r2"
    assert queue.get_task(review_id) is not None
