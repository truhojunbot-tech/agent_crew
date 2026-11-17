"""#1029 follow-up: the evidence key must survive POST /tasks admission.

The #1029 tests drove ``tokenomics_canary`` directly, so they never saw the
``TASK_CONTEXT_KEYS`` allowlist (#941) refuse ``evidence_rereview_comment_url``
with 422 before the canary ran. These go through the FastAPI app.
"""
import asyncio
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_crew import server as sv
from agent_crew import tokenomics_canary as canary
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app

REPO = "truhojunbot-tech/agent_crew"
SHA = "d" * 40
PR = 7
URL = f"https://github.com/{REPO}/pull/{PR}#issuecomment-555"
HEADERS = {"X-Agent-Crew-Project": "demo"}


def _review_payload(task_id="review-impl-77-r1", **context):
    return {"task_id": task_id, "task_type": "review",
            "description": f"Review PR #{PR} for task impl-77.", "branch": "feat/x",
            "priority": 3, "pr_number": PR,
            "context": {"risk_tier": 1, "prev_task_id": "impl-77", "pr_number": PR,
                        "repo": REPO, "reviewed_sha": SHA,
                        "allow_duplicate_review": True, **context}}


def _app(tmp_db):
    return create_app(tmp_db, project="demo", pane_map={"reviewer": "%200"},
                      worktree_map={}, push_fn=lambda *_a: None,
                      watchdog_disabled=True, anomaly_disabled=True)


def test_review_with_evidence_key_is_admitted_and_stored(tmp_db):
    with TestClient(_app(tmp_db)) as client:
        resp = client.post("/tasks", json=_review_payload(
            **{canary.EVIDENCE_URL_KEY: URL}), headers=HEADERS)
    assert resp.status_code == 201, resp.text
    stored = TaskQueue(tmp_db).get_task("review-impl-77-r1")
    assert stored is not None
    assert stored.context["evidence_rereview_comment_url"] == URL


def test_unknown_key_is_still_refused(tmp_db):
    with TestClient(_app(tmp_db)) as client:
        resp = client.post("/tasks", json=_review_payload(not_a_real_key=1),
                           headers=HEADERS)
    assert resp.status_code == 422
    assert resp.json()["detail"] == "unknown_context_key:not_a_real_key"
    assert TaskQueue(tmp_db).get_task("review-impl-77-r1") is None


def test_admitted_evidence_reaches_the_canary_skip(tmp_db, monkeypatch):
    """Standing request_changes on the SHA + verified comment → dispatched, not suppressed."""
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id="rev-standing", task_type="review", description="r",
                              branch="feat/x",
                              context={"pr_number": PR, "reviewed_sha": SHA}))
    queue.submit_result("rev-standing", TaskResult(
        task_id="rev-standing", status="completed", summary="reviewed",
        verdict="request_changes", findings=["f1"], pr_number=PR))
    monkeypatch.setenv(canary.CANARY_ENV, "impl-77")
    seen = []

    def evidence(url):
        seen.append(url)
        return {"repo": REPO, "pr_number": PR, "created_at": time.time() + 60,
                "body": f"re-review {SHA} please"}
    monkeypatch.setattr(canary, "github_comment_evidence", evidence)

    app = _app(tmp_db)
    with TestClient(app) as client:
        resp = client.post("/tasks", json=_review_payload(
            **{canary.EVIDENCE_URL_KEY: URL}), headers=HEADERS)
        assert resp.status_code == 201, resp.text
        task = queue.dequeue(role="reviewer")
        assert task is not None and task.task_id == "review-impl-77-r1"
        worktree = Path(tmp_db).parent / "canary-review-worktree"
        worktree.mkdir(exist_ok=True)
        provider_boundary = RuntimeError("provider would launch")
        dispatched = False
        with patch.object(sv, "_WORKTREE_SYNC_DISABLED", True), patch.object(
            sv, "_ensure_role_protocol", side_effect=provider_boundary
        ):
            try:
                asyncio.run(app.state.dispatch_task(
                    task, "reviewer", dispatch_agent="codex",
                    dispatch_worktree=str(worktree)))
            except RuntimeError as exc:
                assert exc is provider_boundary
                dispatched = True

    assert seen == [URL]
    assert dispatched, "the coordinator's evidence comment did not re-open the review"
    assert queue.get_result("review-impl-77-r1") is None
    receipt = queue.get_tokenomics_shadow_receipt("review-impl-77-r1")
    assert receipt["canary_applied"] == 0
    assert receipt["canary_reason"] == "coordinator_evidence_rereview"
