"""Upstream agent identity propagation and no cross-provider retry (#117/#308)."""
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
# Provider exhaustion never substitutes an upstream agent
# ---------------------------------------------------------------------------


class TestReviewNoProviderSubstitution:
    def test_review_limit_does_not_substitute_an_upstream_agent(self, tmp_db):
        """A rate-limited review must not move to another provider (#308)."""
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
            assert not any(t["task_id"].startswith("fallback-") for t in tasks)
            assert not any(t["context"].get("agent_override") == "gemini"
                           for t in tasks if t["task_id"] != "rev-001")


class TestTestNoProviderSubstitution:
    def test_gemini_failure_does_not_substitute_implementer_or_reviewer(
        self, tmp_db
    ):
        """A failed Gemini test cannot reroute to either upstream provider."""
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
            assert not any(t["context"].get("agent_override") in {"codex", "claude"}
                           for t in tasks if t["task_id"] != "test-001")
