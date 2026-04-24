"""Integration tests for #54 auto-transition: impl completion → auto-enqueue review.

Tests the end-to-end flow where completing an impl task automatically creates
and enqueues a review task, independent of CLI timeout.
"""
import pytest
from fastapi.testclient import TestClient

from agent_crew.server import create_app


pytestmark = pytest.mark.integration


# I-AR01: impl completed → auto-enqueue review with prev_task_id
def test_i_ar01_impl_completion_auto_enqueues_review(test_client):
    """Test that submitting impl result auto-creates a review task."""
    impl_payload = {
        "task_id": "impl-ar01",
        "task_type": "implement",
        "description": "Implement feature X",
        "branch": "feat/x",
        "priority": 2,
        "context": {},
    }
    resp = test_client.post("/tasks", json=impl_payload)
    assert resp.status_code == 201

    # Submit impl result as completed
    result_payload = {
        "task_id": "impl-ar01",
        "status": "completed",
        "summary": "Feature X implemented",
        "findings": [],
        "verdict": None,
        "pr_number": None,
    }
    resp = test_client.post("/tasks/impl-ar01/result", json=result_payload)
    assert resp.status_code == 200

    # Verify review task was auto-created
    tasks = test_client.get("/tasks").json()
    review_tasks = [t for t in tasks if t["task_type"] == "review"]
    assert len(review_tasks) >= 1

    review = review_tasks[0]
    assert review["description"] == "Implement feature X"
    assert review["branch"] == "feat/x"
    assert "impl-ar01" in str(review.get("context", {}))


# I-AR02: impl failed → no auto-enqueue review
def test_i_ar02_impl_failed_no_auto_review(test_client):
    """Test that failed impl tasks do not trigger auto-review."""
    impl_payload = {
        "task_id": "impl-ar02",
        "task_type": "implement",
        "description": "Implement feature Y",
        "branch": "feat/y",
        "priority": 2,
        "context": {},
    }
    test_client.post("/tasks", json=impl_payload)

    # Submit impl result as failed
    result_payload = {
        "task_id": "impl-ar02",
        "status": "failed",
        "summary": "Implementation failed",
        "findings": [],
    }
    resp = test_client.post("/tasks/impl-ar02/result", json=result_payload)
    assert resp.status_code == 200

    # Verify NO review task was created
    tasks = test_client.get("/tasks").json()
    review_tasks = [t for t in tasks if t["task_type"] == "review"]
    assert all(t.get("description") != "Implement feature Y" for t in review_tasks)


# I-AR03: auto-created review preserves checklist context
def test_i_ar03_auto_review_preserves_checklist(test_client):
    """Test that auto-created review task includes proper checklist context."""
    impl_payload = {
        "task_id": "impl-ar03",
        "task_type": "implement",
        "description": "Implement feature Z",
        "branch": "feat/z",
        "priority": 1,
        "context": {"custom": "context"},
    }
    test_client.post("/tasks", json=impl_payload)

    result_payload = {
        "task_id": "impl-ar03",
        "status": "completed",
        "summary": "Completed",
        "findings": [],
    }
    test_client.post("/tasks/impl-ar03/result", json=result_payload)

    # Get auto-created review task
    tasks = test_client.get("/tasks").json()
    review_tasks = [t for t in tasks if t["task_type"] == "review" and t.get("description") == "Implement feature Z"]
    assert len(review_tasks) == 1

    review = review_tasks[0]
    ctx = review.get("context", {})
    assert ctx.get("prev_task_id") == "impl-ar03"
    assert "checklist_layers" in ctx
    assert "reviewer_rejects_happy_path_only" in ctx
    assert ctx["checklist_layers"] == ["test_quality", "code_quality", "business_gap"]


# I-AR04: multiple impl tasks each create their own review
def test_i_ar04_multiple_impl_tasks_create_reviews(test_client):
    """Test that multiple impl tasks each trigger their own review creation."""
    for i in range(2):
        impl_payload = {
            "task_id": f"impl-ar04-{i}",
            "task_type": "implement",
            "description": f"Implement feature {i}",
            "branch": f"feat/{i}",
            "priority": 2,
            "context": {},
        }
        test_client.post("/tasks", json=impl_payload)

        result_payload = {
            "task_id": f"impl-ar04-{i}",
            "status": "completed",
            "summary": f"Completed feature {i}",
            "findings": [],
        }
        test_client.post(f"/tasks/impl-ar04-{i}/result", json=result_payload)

    # Verify 2 review tasks were created
    tasks = test_client.get("/tasks").json()
    review_tasks = [t for t in tasks if t["task_type"] == "review"]
    feature_reviews = [t for t in review_tasks if "feature" in t.get("description", "").lower()]
    assert len(feature_reviews) >= 2
