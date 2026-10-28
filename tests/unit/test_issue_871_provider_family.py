"""#871: review independence compares provider families, not raw agent strings."""

import json

import pytest
from fastapi.testclient import TestClient

import agent_crew.server as server
from agent_crew import github
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.role_mapping import provider_family


SHA = "b" * 40


@pytest.mark.parametrize("agent,family", [
    ("claude", "claude"), ("claude_cloud", "claude"), (" Claude-Opus ", "claude"),
    ("claude_x", "claude"), ("codex", "codex"), ("codex_cloud", "codex"),
    ("gemini", "gemini"), ("gemini-2", "gemini"), (" script ", "script"),
    ("claudex", "claudex"), ("", ""), (None, ""),
])
def test_provider_family(agent, family):
    assert provider_family(agent) == family


def _app(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "project": "demo",
        "role_agents": {"implementer": "claude", "reviewer": "codex", "tester": "script"},
    }))
    return server.create_app(
        db_path=str(tmp_path / "tasks.db"), state_path=str(state_path),
        watchdog_disabled=True, anomaly_disabled=True)


@pytest.mark.parametrize("implementer,reviewer,refused", [
    ("claude", "claude_cloud", True),
    ("codex", "codex", True),
    ("claude", "codex", False),
    ("codex", "claude_cloud", False),
])
def test_dispatch_guard_uses_provider_family(tmp_path, implementer, reviewer, refused):
    app = _app(tmp_path)
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(
        task_id="review-871", task_type="review", description="review",
        project="demo", context={"implementer_agent": implementer}))
    review = queue.dequeue(role="reviewer")
    with TestClient(app):
        assert app.state.refuse_self_review(review, reviewer) is refused
    result = queue.get_result("review-871")
    if refused:
        assert result is not None
        assert result.status == "needs_human"
        # The reason keeps the original agent string, not the family.
        assert result.summary.startswith(f"reviewer_matches_implementer: {reviewer}")
    else:
        assert result is None


def _approved_review(queue, author, reviewer):
    queue.enqueue(TaskRequest(task_id="impl", task_type="implement", description="impl",
                              branch="fix/871", project="agent_crew"))
    queue.record_attribution("impl", agent=author, task_type="implement")
    queue.enqueue(TaskRequest(
        task_id="review", task_type="review", description="review", branch="fix/871",
        project="agent_crew",
        context={"pr_number": 871, "reviewed_sha": SHA, "prev_task_id": "impl"}))
    with queue._connect() as db:
        db.execute("UPDATE tasks SET status='completed', verdict='approve', "
                   "pr_number=871 WHERE task_id='review'")
    queue.record_attribution("review", agent=reviewer, task_type="review")


@pytest.mark.parametrize("author,reviewer,independent", [
    ("claude", "claude_cloud", False),
    ("codex", "codex", False),
    ("claude", "codex", True),
    ("codex", "claude_cloud", True),
])
def test_merge_gate_uses_provider_family(tmp_path, monkeypatch, author, reviewer, independent):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _approved_review(queue, author, reviewer)
    monkeypatch.setattr(github, "pr_head_sha", lambda *_args, **_kwargs: SHA)

    expected = ((SHA, reviewer, "review", "independent approval") if independent else
                ("", "", "", f"reviewer agent equals implementer agent ({reviewer})"))
    assert github.independent_review_for_head(queue, 871, "owner/repo") == expected
