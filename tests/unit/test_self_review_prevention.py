"""Self-review/self-test prevention in rate-limit fallback (Issue #117).

When `_auto_fallback_failed_task` reroutes a review or test task because
the assigned agent hit a rate-limit, the successor must skip any agent
that already participated upstream in the pipeline:

  - review fallback must skip the impl task's implementer
  - test fallback must skip both the implementer and the reviewer

To make that decision, `_auto_enqueue_review` and `_auto_enqueue_test`
record the upstream agent identities into the new task's context
(`implementer_agent`, `reviewer_agent`). The fallback handler then
unions those names into the `excluded` list before walking the chain.
"""
from fastapi.testclient import TestClient

from agent_crew.server import create_app


def _task_payload(task_id, task_type, description="do work", priority=3,
                  project="", context=None):
    return {
        "task_id": task_id,
        "task_type": task_type,
        "description": description,
        "branch": "main",
        "priority": priority,
        "context": context or {},
        "project": project,
    }


def _result_payload(task_id, status="completed", summary="done",
                    verdict=None, findings=None, pr_number=None):
    return {
        "task_id": task_id,
        "status": status,
        "summary": summary,
        "verdict": verdict,
        "findings": findings or [],
        "pr_number": pr_number,
    }


class RecordingPush:
    def __init__(self):
        self.calls = []

    def __call__(self, pane_id, text):
        self.calls.append((pane_id, text))


# Full 3-agent pane map: each role + agent share a pane (matches `crew setup`).
PANE_MAP = {
    "implementer": "%100", "claude": "%100",
    "reviewer": "%200", "codex": "%200",
    "tester": "%300", "gemini": "%300",
}


# ---------------------------------------------------------------------------
# auto_enqueue_review records implementer
# ---------------------------------------------------------------------------


class TestAutoEnqueueReviewRecordsImplementer:
    def test_default_implementer_is_claude(self, tmp_db):
        """Impl task without agent_override → review_context records claude."""
        push = RecordingPush()
        app = create_app(
            db_path=tmp_db, pane_map=PANE_MAP, port=8100, push_fn=push
        )
        with TestClient(app) as client:
            client.post("/tasks", json=_task_payload("impl-001", "implement"))
            client.post(
                "/tasks/impl-001/result", json=_result_payload("impl-001")
            )
            tasks = client.get("/tasks").json()
            reviews = [t for t in tasks if t["task_type"] == "review"]
            assert len(reviews) == 1
            assert reviews[0]["context"].get("implementer_agent") == "claude"

    def test_override_implementer_is_recorded(self, tmp_db):
        """Impl task with agent_override=gemini → review records gemini."""
        push = RecordingPush()
        app = create_app(
            db_path=tmp_db, pane_map=PANE_MAP, port=8100, push_fn=push
        )
        with TestClient(app) as client:
            ctx = {"agent_override": "gemini"}
            client.post(
                "/tasks",
                json=_task_payload("impl-002", "implement", context=ctx),
            )
            client.post(
                "/tasks/impl-002/result", json=_result_payload("impl-002")
            )
            tasks = client.get("/tasks").json()
            reviews = [t for t in tasks if t["task_type"] == "review"]
            assert reviews[0]["context"].get("implementer_agent") == "gemini"


# ---------------------------------------------------------------------------
# auto_enqueue_test records both implementer and reviewer
# ---------------------------------------------------------------------------


class TestAutoEnqueueTestRecordsBothAgents:
    def test_full_pipeline_records_implementer_and_reviewer(self, tmp_db):
        """impl(claude) → review(codex approve) → test must inherit
        implementer_agent=claude and reviewer_agent=codex."""
        push = RecordingPush()
        app = create_app(
            db_path=tmp_db, pane_map=PANE_MAP, port=8100, push_fn=push
        )
        with TestClient(app) as client:
            client.post("/tasks", json=_task_payload("impl-003", "implement"))
            client.post(
                "/tasks/impl-003/result", json=_result_payload("impl-003")
            )
            tasks = client.get("/tasks").json()
            review = [t for t in tasks if t["task_type"] == "review"][0]
            client.post(
                f"/tasks/{review['task_id']}/result",
                json=_result_payload(review["task_id"], verdict="approve"),
            )
            tasks2 = client.get("/tasks").json()
            tests = [t for t in tasks2 if t["task_type"] == "test"]
            assert len(tests) == 1
            test_ctx = tests[0]["context"]
            assert test_ctx.get("implementer_agent") == "claude"
            assert test_ctx.get("reviewer_agent") == "codex"


# ---------------------------------------------------------------------------
# Fallback skips upstream agents
# ---------------------------------------------------------------------------


class TestReviewFallbackSkipsImplementer:
    def test_codex_review_fails_claude_skipped_picks_gemini(self, tmp_db):
        """Review chain ['codex','claude','gemini']. codex (current) hits
        rate-limit and claude is the implementer → fallback must pick gemini."""
        push = RecordingPush()
        app = create_app(
            db_path=tmp_db, pane_map=PANE_MAP, port=8100, push_fn=push
        )
        with TestClient(app) as client:
            ctx = {"agent_override": "codex", "implementer_agent": "claude"}
            client.post(
                "/tasks",
                json=_task_payload("rev-001", "review", context=ctx),
            )
            client.post(
                "/tasks/rev-001/result",
                json=_result_payload(
                    "rev-001", status="failed", summary="usage limit"
                ),
            )
            tasks = client.get("/tasks").json()
            fb = [
                t for t in tasks
                if t["task_id"].startswith("fallback-rev-001")
            ]
            assert len(fb) == 1, (
                "expected exactly one fallback task; got "
                f"{[t['task_id'] for t in fb]}"
            )
            new_ctx = fb[0]["context"]
            assert new_ctx["agent_override"] == "gemini"
            excluded = new_ctx.get("fallback_excluded", [])
            assert "codex" in excluded
            assert "claude" in excluded
            # implementer_agent must propagate so subsequent fallbacks still
            # know who not to route back to.
            assert new_ctx.get("implementer_agent") == "claude"


class TestTestFallbackSkipsImplementerAndReviewer:
    def test_gemini_test_fails_implementer_and_reviewer_excluded_escalates(
        self, tmp_db
    ):
        """Test chain ['gemini','codex','claude']. gemini (current) hits
        rate-limit; codex was the reviewer; claude was the implementer →
        chain exhausted → no fallback task, escalation gate created."""
        push = RecordingPush()
        app = create_app(
            db_path=tmp_db, pane_map=PANE_MAP, port=8100, push_fn=push
        )
        with TestClient(app) as client:
            ctx = {
                "agent_override": "gemini",
                "implementer_agent": "claude",
                "reviewer_agent": "codex",
            }
            client.post(
                "/tasks",
                json=_task_payload("test-001", "test", context=ctx),
            )
            client.post(
                "/tasks/test-001/result",
                json=_result_payload(
                    "test-001",
                    status="failed",
                    summary="quota exceeded",
                ),
            )
            tasks = client.get("/tasks").json()
            fb = [
                t for t in tasks
                if t["task_id"].startswith("fallback-test-001")
            ]
            assert fb == [], (
                "expected zero fallback tasks (chain exhausted); got "
                f"{[t['task_id'] for t in fb]}"
            )
            gates = client.get("/gates/pending").json()
            esc = [g for g in gates if g.get("type") == "escalation"]
            assert len(esc) >= 1
