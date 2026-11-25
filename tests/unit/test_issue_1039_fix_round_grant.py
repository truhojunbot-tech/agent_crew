"""#1039 — a per-lineage ``fix_round_grant`` narrows the automated fix budget.

The only code-enforced limit on automatic fix rounds was the port ceiling
``AGENT_CREW_REVIEW_FIX_MAX_ROUNDS`` (#244). A grant written in a #44 row or a
Quota exception ("1 implement + 1 fix round") never reached the task, so the
crew enqueued rounds up to the ceiling and coordinators cancelled the overrun
by hand. The grant now rides in the task context, is carried to every
successor, and ``auto_enqueue_fix`` takes ``min(ceiling, grant)``.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from agent_crew.pipeline import (
    FIX_EXHAUSTED_MARKER, auto_enqueue_fix, auto_enqueue_review, auto_enqueue_test,
    fix_task_id, lineage_fix_round_grant,
)
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import MalformedStoredRow, TaskQueue
from agent_crew.server import create_app

FINDING = "HIGH trader.py:12 - order size unchecked"
BRANCH = "agent/codex/1039-x"
REPO = "truhojunbot-tech/agent_crew"
PR = 1039


def _open(pr):
    return "open"


def _notify(sent):
    return lambda m: sent.append(m) or True


@pytest.fixture
def q(tmp_db, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "3")
    return TaskQueue(tmp_db)


def _implement(q, context):
    impl_id = f"impl-{uuid.uuid4().hex[:8]}"
    q.enqueue(TaskRequest(task_id=impl_id, task_type="implement",
                          description="implement", branch=BRANCH,
                          context={"crew_run_branch": True, **context}))
    q.submit_result(impl_id, TaskResult(task_id=impl_id, status="completed",
                                        summary="done", pr_number=PR))
    return impl_id


def _request_changes(q, prev_task_id, context):
    review_id = f"review-{uuid.uuid4().hex[:8]}"
    q.enqueue(TaskRequest(task_id=review_id, task_type="review", description="review",
                          branch=BRANCH,
                          context={"prev_task_id": prev_task_id, "pr_number": PR, **context}))
    q.submit_result(review_id, TaskResult(
        task_id=review_id, status="completed", summary="request_changes",
        verdict="request_changes", findings=[FINDING], pr_number=PR))
    return review_id


def _fix(q, review_id, *, sent, comments):
    return auto_enqueue_fix(
        q, review_id, pr_state_fn=_open, repo=REPO,
        comment_fn=lambda pr, body: comments.append((pr, body)),
        already_announced_fn=lambda pr, marker: False,
        coordinator_notify_fn=_notify(sent))


def _events(q, task_id):
    return [e["event"] for e in q.get_exec_state(task_id)["events"]]


def test_grant_one_allows_exactly_one_fix_round_then_exhausts(q):
    sent, comments = [], []
    impl_id = _implement(q, {"fix_round_grant": 1})

    # Round 1: the review of the root implement carries no grant of its own —
    # the lineage walk must find it on the root.
    review1 = _request_changes(q, impl_id, {})
    fix1 = _fix(q, review1, sent=sent, comments=comments)
    assert fix1 == fix_task_id(review1, 1)
    fix_ctx = q.get_task(fix1).context
    assert fix_ctx["fix_round"] == 1 and fix_ctx["fix_round_grant"] == 1
    assert sent == [] and comments == []

    # Round 2: the second request_changes is over the grant (port cap is 3).
    review2 = _request_changes(q, fix1, {"fix_round": 1})
    for _ in range(2):  # a replayed result POST must not announce twice
        assert _fix(q, review2, sent=sent, comments=comments) is None

    fixes = [t for t in q.list_tasks() if t.task_type == "implement" and t.task_id != impl_id]
    assert [t.task_id for t in fixes] == [fix1]
    ctx = q.get_task(review2).context
    assert ctx["lineage_terminal_reason"] == "successor_refused: fix_grant_exhausted"
    assert _events(q, review2).count("fix_grant_exhausted") == 1
    assert len(sent) == 1
    assert "kind=successor_refused" in sent[0]
    assert "fix_grant_exhausted" in sent[0] and "round=2 max=1 grant=1" in sent[0]
    assert len(comments) == 1
    assert comments[0][0] == PR and FIX_EXHAUSTED_MARKER in comments[0][1]


def test_grant_zero_creates_no_fix_round(q):
    sent, comments = [], []
    impl_id = _implement(q, {"fix_round_grant": 0})
    review = _request_changes(q, impl_id, {})

    assert _fix(q, review, sent=sent, comments=comments) is None

    assert [t for t in q.list_tasks() if t.task_type == "implement"
            and t.task_id != impl_id] == []
    assert q.get_task(review).context["lineage_terminal_reason"] == (
        "successor_refused: fix_grant_exhausted")
    assert "fix_grant_exhausted" in _events(q, review)
    assert len(sent) == 1 and "round=1 max=0 grant=0" in sent[0]
    assert len(comments) == 1


def test_absent_grant_keeps_the_port_ceiling(q):
    sent, comments = [], []
    impl_id = _implement(q, {})

    under = _request_changes(q, impl_id, {"fix_round": 2})
    assert _fix(q, under, sent=sent, comments=comments) == fix_task_id(under, 3)
    assert "fix_round_grant" not in q.get_task(fix_task_id(under, 3)).context

    over = _request_changes(q, impl_id, {"fix_round": 3})
    assert _fix(q, over, sent=sent, comments=comments) is None
    assert q.get_task(over).context["lineage_terminal_reason"] == (
        "successor_refused: fix_rounds_exhausted")
    assert "fix_grant_exhausted" not in _events(q, over)
    assert len(sent) == 1 and "round=4 max=3" in sent[0] and "grant=" not in sent[0]


def test_grant_above_the_port_ceiling_is_bounded_by_the_ceiling(q):
    sent, comments = [], []
    impl_id = _implement(q, {"fix_round_grant": 5})

    under = _request_changes(q, impl_id, {"fix_round": 2})
    assert _fix(q, under, sent=sent, comments=comments) == fix_task_id(under, 3)
    assert q.get_task(fix_task_id(under, 3)).context["fix_round_grant"] == 5

    over = _request_changes(q, impl_id, {"fix_round": 3})
    assert _fix(q, over, sent=sent, comments=comments) is None
    # The ceiling, not the grant, ended the lineage: no grant event.
    assert q.get_task(over).context["lineage_terminal_reason"] == (
        "successor_refused: fix_rounds_exhausted")
    assert "fix_grant_exhausted" not in _events(q, over)
    assert len(sent) == 1 and "round=4 max=3 grant=5" in sent[0]


def test_successor_with_a_larger_grant_is_ignored(q):
    """The lineage minimum wins: a successor cannot widen the root's grant."""
    sent, comments = [], []
    impl_id = _implement(q, {"fix_round_grant": 1})
    review1 = _request_changes(q, impl_id, {})
    fix1 = _fix(q, review1, sent=sent, comments=comments)
    assert fix1 == fix_task_id(review1, 1)

    review2 = _request_changes(q, fix1, {"fix_round": 1, "fix_round_grant": 5})
    assert _fix(q, review2, sent=sent, comments=comments) is None
    assert q.get_task(review2).context["lineage_terminal_reason"] == (
        "successor_refused: fix_grant_exhausted")
    assert len(sent) == 1 and "round=2 max=1 grant=1" in sent[0]


def test_successor_with_a_smaller_grant_narrows(q):
    sent, comments = [], []
    impl_id = _implement(q, {"fix_round_grant": 3})
    review = _request_changes(q, impl_id, {"fix_round": 1, "fix_round_grant": 1})
    assert _fix(q, review, sent=sent, comments=comments) is None
    assert "round=2 max=1 grant=1" in sent[0]


def test_malformed_grant_is_reported_not_widened(q):
    impl_id = _implement(q, {})
    q.merge_task_context(impl_id, {"fix_round_grant": "two"})
    review = _request_changes(q, impl_id, {})
    with pytest.raises(MalformedStoredRow):
        auto_enqueue_fix(q, review, pr_state_fn=_open)


def test_lineage_fix_round_grant_takes_the_minimum_over_ancestors():
    def task(task_id, context):
        return TaskRequest(task_id=task_id, task_type="implement", description="d",
                           branch=BRANCH, context=context)
    root = task("root", {"fix_round_grant": 2})
    mid = task("mid", {"prev_task_id": "root", "fix_round_grant": 7})
    leaf = task("leaf", {"prev_task_id": "mid"})
    by_id = {t.task_id: t for t in (root, mid, leaf)}
    assert lineage_fix_round_grant(by_id, leaf) == 2
    assert lineage_fix_round_grant({"leaf": leaf}, leaf) is None


def test_review_and_test_successors_carry_the_grant(q):
    impl_id = _implement(q, {"fix_round_grant": 1})
    review_id = auto_enqueue_review(q, impl_id, pr_state_fn=_open)
    assert review_id is not None
    assert q.get_task(review_id).context["fix_round_grant"] == 1

    q.submit_result(review_id, TaskResult(
        task_id=review_id, status="completed", summary="lgtm", verdict="approve",
        findings=[], pr_number=PR))
    test_id = auto_enqueue_test(q, review_id, pr_state_fn=_open)
    assert test_id is not None
    assert q.get_task(test_id).context["fix_round_grant"] == 1


def _payload(**context):
    return {"task_id": "impl-1039-grant", "task_type": "implement",
            "description": "implement #1039", "branch": BRANCH, "priority": 2,
            "context": {"repo": REPO, **context}}


def _app(tmp_db):
    return create_app(tmp_db, project="demo", pane_map={"implementer": "%200"},
                      worktree_map={}, push_fn=lambda *_a: None,
                      watchdog_disabled=True, anomaly_disabled=True)


def test_post_tasks_admits_fix_round_grant(tmp_db):
    with TestClient(_app(tmp_db)) as client:
        resp = client.post("/tasks", json=_payload(fix_round_grant=1),
                           headers={"X-Agent-Crew-Project": "demo"})
    assert resp.status_code == 201, resp.text
    assert TaskQueue(tmp_db).get_task("impl-1039-grant").context["fix_round_grant"] == 1


@pytest.mark.parametrize("bad", ["1", -1, 1.5, True])
def test_post_tasks_refuses_a_malformed_grant(tmp_db, bad):
    with TestClient(_app(tmp_db)) as client:
        resp = client.post("/tasks", json=_payload(fix_round_grant=bad),
                           headers={"X-Agent-Crew-Project": "demo"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "fix_round_grant must be an integer >= 0"
    assert TaskQueue(tmp_db).get_task("impl-1039-grant") is None
