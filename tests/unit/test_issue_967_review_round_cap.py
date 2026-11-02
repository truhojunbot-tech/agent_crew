"""Review admission respects a provider's lineage budget (#967)."""

import pytest

from agent_crew.pipeline import (ReviewRoundCapReached, auto_enqueue_fix, auto_enqueue_review,
                                 requeue_review_at_head)
from agent_crew.protocol import TaskRequest, TaskResult
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


@pytest.mark.parametrize("explicit_override", [None, "claude_cloud"])
def test_requeue_without_cap_preserves_only_explicit_provider_override(
        tmp_path, monkeypatch, explicit_override):
    queue = _queue(tmp_path)
    monkeypatch.delenv("AGENT_CREW_REVIEW_MAX_ROUNDS_CLAUDE", raising=False)
    monkeypatch.delenv("AGENT_CREW_REVIEW_MAX_ROUNDS_CLAUDE_CLOUD", raising=False)
    base = {"risk_tier": 1, "branch": "feature"}
    if explicit_override:
        base["agent_override"] = explicit_override
    review_id = requeue_review_at_head(
        queue, "prior-1", 7, "b" * 40, base, project="demo",
        reviewer_provider="codex")
    assert review_id == "review-7-bbbbbbbbbbbb"
    context = queue.get_task(review_id).context
    assert context.get("agent_override") == explicit_override


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


def test_post_approval_test_fix_gets_one_delta_review_with_bounded_fix_rounds(
        tmp_path, monkeypatch):
    approved_sha, new_head = "a" * 40, "b" * 40
    monkeypatch.setenv("AGENT_CREW_REVIEW_MAX_ROUNDS_CLAUDE", "2")
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "2")
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    def add(task_id, kind, parent=None, *, fix_round=0, sha=""):
        context = {"risk_tier": 1, "lineage_root_task_id": "root",
                   "repo": "owner/repo", "pr_number": 7, "fix_round": fix_round}
        if parent:
            context["prev_task_id"] = parent
        if sha:
            context["reviewed_sha"] = sha
        if kind == "implement":
            context["crew_run_branch"] = True
        queue.enqueue(TaskRequest(task_id=task_id, task_type=kind,
                                  branch="feature", description="fixture", context=context))
    add("root", "implement")
    add("r0", "review", "root")
    queue.submit_result("r0", TaskResult(task_id="r0", status="completed",
                                         summary="changes", verdict="request_changes",
                                         findings=["HIGH x.py:1 - fix"]))
    add("fix1", "implement", "r0", fix_round=1)
    add("r1", "review", "fix1", fix_round=1, sha=approved_sha)
    queue.submit_result("r1", TaskResult(task_id="r1", status="completed",
                                         summary="approved", verdict="approve"))
    add("test1", "test", "r1", fix_round=1, sha=approved_sha)
    queue.submit_result("test1", TaskResult(
        task_id="test1", status="failed", summary="new failure",
        error_info={"kind": "test_failure", "reason": "script_test_new_failures",
                    "script_test": {"outcome": "FAILED", "reason": "new_failures",
                                    "new_failures": ["tests/test_x.py::test_x"]}}))
    add("fix2", "implement", "test1", fix_round=2)
    outcome = auto_enqueue_review(queue, "fix2", 7, reviewer_provider="claude",
        pr_state_fn=lambda _: "open", result=TaskResult(
            task_id="fix2", status="completed", summary="fixed", commit=new_head))
    assert outcome == "review-fix2-r2"
    delta = queue.get_task(outcome)
    assert delta.context["delta_review"] is True
    assert delta.context["approved_sha"] == approved_sha
    assert delta.context["reviewed_sha"] == new_head
    assert approved_sha in delta.context["instructions"]
    assert new_head in delta.context["instructions"]
    assert auto_enqueue_review(queue, "fix2", 7, reviewer_provider="claude",
        pr_state_fn=lambda _: "open", result=TaskResult(
            task_id="fix2", status="completed", summary="fixed", commit=new_head)) == outcome
    assert len([t for t in queue.list_tasks() if t.task_type == "review"]) == 3

    queue.submit_result(outcome, TaskResult(task_id=outcome, status="completed",
                                            summary="approved", verdict="approve"))
    add("test2", "test", outcome, fix_round=2, sha=new_head)
    queue.submit_result("test2", TaskResult(
        task_id="test2", status="failed", summary="another failure",
        findings=["tests/test_y.py::test_y failed"],
        error_info={"kind": "test_failure", "reason": "script_test_new_failures",
                    "script_test": {"outcome": "FAILED", "reason": "new_failures",
                                    "new_failures": ["tests/test_y.py::test_y"]}}))
    assert auto_enqueue_fix(queue, "test2", pr_state_fn=lambda _: "open",
                            head_sha_fn=lambda *_a, **_k: new_head) is None
    assert not any(t.task_type == "implement" and t.context.get("fix_round") == 3
                   for t in queue.list_tasks())
