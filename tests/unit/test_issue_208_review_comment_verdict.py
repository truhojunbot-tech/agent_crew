"""Issue #208: a review task that completes with verdict=null + no findings
(a "clean approve" per the #100 defensive resolver) must post a PR comment
header that says approve, not request_changes.

Root cause: the GitHub-comment call site passed the raw, unresolved
`result.verdict` while the auto-enqueue-test decision eight lines below it
already used `_resolve_verdict(result)`. The two diverged whenever a
reviewer left the structured `verdict` field null but said "approve" in
prose (or simply reported no findings).
"""
from fastapi.testclient import TestClient

from agent_crew.server import create_app


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
        port=8201,
        push_fn=push,
        watchdog_disabled=True,
    )


def _review_payload(task_id, pr_number):
    return {
        "task_id": task_id,
        "task_type": "review",
        "description": "Review PR",
        "branch": "agent/agent_crew/claude",
        "priority": 3,
        "context": {"pr_number": pr_number},
        "project": "",
    }


def _result_payload(task_id, verdict, findings, summary="looks good"):
    return {
        "task_id": task_id,
        "status": "completed",
        "summary": summary,
        "verdict": verdict,
        "findings": findings,
        "pr_number": None,
    }


def test_u208_null_verdict_no_findings_posts_approve_comment(tmp_db, monkeypatch):
    calls = []

    def fake_post_review_comment(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr("agent_crew.github.post_review_comment", fake_post_review_comment)

    push_calls: list = []
    app = _make_app(tmp_db, push_calls)
    with TestClient(app) as client:
        client.post("/tasks", json=_review_payload("review-208a", pr_number=99))
        client.post(
            "/tasks/review-208a/result",
            json=_result_payload("review-208a", verdict=None, findings=[]),
        )

    assert len(calls) == 1
    assert calls[0]["verdict"] == "approve"


def test_u208_null_verdict_with_findings_still_posts_request_changes(tmp_db, monkeypatch):
    calls = []

    def fake_post_review_comment(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr("agent_crew.github.post_review_comment", fake_post_review_comment)

    push_calls: list = []
    app = _make_app(tmp_db, push_calls)
    with TestClient(app) as client:
        client.post("/tasks", json=_review_payload("review-208b", pr_number=99))
        client.post(
            "/tasks/review-208b/result",
            json=_result_payload("review-208b", verdict=None, findings=["real bug found"]),
        )

    assert len(calls) == 1
    assert calls[0]["verdict"] == "request_changes"


def test_u208_explicit_request_changes_verdict_passed_through(tmp_db, monkeypatch):
    calls = []

    def fake_post_review_comment(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr("agent_crew.github.post_review_comment", fake_post_review_comment)

    push_calls: list = []
    app = _make_app(tmp_db, push_calls)
    with TestClient(app) as client:
        client.post("/tasks", json=_review_payload("review-208c", pr_number=99))
        client.post(
            "/tasks/review-208c/result",
            json=_result_payload("review-208c", verdict="request_changes", findings=["x"]),
        )

    assert len(calls) == 1
    assert calls[0]["verdict"] == "request_changes"
