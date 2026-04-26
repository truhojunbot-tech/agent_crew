"""Reviewer must be told to read the live PR head, not stale line numbers
from earlier rounds (Issue #86)."""
from fastapi.testclient import TestClient

from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _impl_payload(task_id="impl-1"):
    return {
        "task_id": task_id,
        "task_type": "implement",
        "description": "Implement Issue #79 — telegram notify helper",
        "branch": "agent/agent_crew/claude",
        "priority": 3,
        "context": {},
        "project": "",
    }


def _completed_result(task_id, pr_number=None):
    return {
        "task_id": task_id,
        "status": "completed",
        "summary": "implementation done",
        "verdict": None,
        "findings": [],
        "pr_number": pr_number,
    }


def _make_app(tmp_db, push_calls):
    panes = {
        "implementer": "%C", "claude": "%C",
        "reviewer": "%X", "codex": "%X",
        "tester": "%G", "gemini": "%G",
    }

    def push(pane_id, text):
        push_calls.append((pane_id, text))

    return create_app(
        db_path=tmp_db,
        pane_map=panes,
        port=8200,
        push_fn=push,
        watchdog_disabled=True,
    )


def test_review_task_carries_pr_number_when_impl_posts_one(tmp_db):
    push_calls: list = []
    app = _make_app(tmp_db, push_calls)
    with TestClient(app) as client:
        client.post("/tasks", json=_impl_payload("impl-86a"))
        client.post(
            "/tasks/impl-86a/result",
            json=_completed_result("impl-86a", pr_number=83),
        )
    review = next(
        t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "review"
    )
    assert review.context["pr_number"] == 83


def test_review_instructions_mention_pr_diff_when_pr_known(tmp_db):
    push_calls: list = []
    app = _make_app(tmp_db, push_calls)
    with TestClient(app) as client:
        client.post("/tasks", json=_impl_payload("impl-86b"))
        client.post(
            "/tasks/impl-86b/result",
            json=_completed_result("impl-86b", pr_number=42),
        )
    review = next(
        t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "review"
    )
    instr = review.context["instructions"]
    assert "gh pr diff 42" in instr
    assert "FRESHNESS" in instr
    assert "do NOT" in instr.lower() or "do not" in instr.lower()
    # Reviewer must be reminded that earlier-round line numbers are stale.
    assert "earlier" in instr.lower() or "prior" in instr.lower()


def test_review_instructions_fallback_when_pr_number_missing(tmp_db):
    push_calls: list = []
    app = _make_app(tmp_db, push_calls)
    with TestClient(app) as client:
        client.post("/tasks", json=_impl_payload("impl-86c"))
        # impl posts result with pr_number=None
        client.post(
            "/tasks/impl-86c/result",
            json=_completed_result("impl-86c", pr_number=None),
        )
    review = next(
        t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "review"
    )
    instr = review.context["instructions"]
    # Even without pr_number, reviewer must be told to look up the PR via
    # the branch and fetch the live diff — not review from a stale local
    # checkout.
    assert "gh pr list --head" in instr
    assert "agent/agent_crew/claude" in instr
    assert "FRESHNESS" in instr


def test_review_pushed_to_reviewer_pane_on_impl_completion(tmp_db):
    push_calls: list = []
    app = _make_app(tmp_db, push_calls)
    with TestClient(app) as client:
        client.post("/tasks", json=_impl_payload("impl-86d"))
        # First push: impl → claude pane (%C)
        assert any(call[0] == "%C" for call in push_calls)
        client.post(
            "/tasks/impl-86d/result",
            json=_completed_result("impl-86d", pr_number=99),
        )
    # Second push: review → codex pane (%X), and the message must contain
    # the freshness directive that caused this whole bug.
    review_pushes = [c for c in push_calls if c[0] == "%X"]
    assert len(review_pushes) == 1
    assert "gh pr diff 99" in review_pushes[0][1]
