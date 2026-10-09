"""#669: targeted implement work is complete after its targeted checks and push."""

import pytest
from fastapi.testclient import TestClient

from agent_crew.instructions import generate
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


SHA = "a" * 40
BRANCH = "agent/669-example"


def test_implementer_targeted_scope_explains_completion(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CREW_TEST_SCOPE", '{"full_suite": false}')
    text = generate("implementer", "example", 8105, delivery="dispatcher",
                    worktree_path=str(tmp_path))
    assert "full suite: not run here (coordinator/CI verifies pre-merge)" in text
    assert "status=completed" in text
    assert "targeted counts" in text
    assert "Never post failed" in text


def test_implementer_full_scope_omits_targeted_rule(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CREW_TEST_SCOPE", '{"full_suite": true}')
    text = generate("implementer", "example", 8105, delivery="dispatcher",
                    worktree_path=str(tmp_path))
    assert "full suite: not run here" not in text


@pytest.mark.parametrize("task_id,summary", [
    ("impl-d7d1a8b5", "targeted 59/59 pass, but full suite not verified green"),
    ("impl-3d52da2b", "targeted tests pass; full suite not verified"),
])
def test_failed_implement_with_pushed_commit_is_not_retried(
        tmp_db, monkeypatch, task_id, summary):
    from agent_crew import github

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setattr(github, "pr_head_sha", lambda *args, **kwargs: SHA)
    monkeypatch.setattr(github, "branch_head_sha", lambda *args, **kwargs: SHA)
    app = create_app(db_path=tmp_db, project="example", watchdog_disabled=True,
                     anomaly_disabled=True)
    with TestClient(app, headers={"X-Agent-Crew-Project": "example"}) as client:
        created = client.post("/tasks", json={
            "task_id": task_id, "task_type": "implement", "description": "implement",
            "branch": BRANCH, "priority": 3, "project": "example",
            "context": {"repo": "owner/repo", "pr_number": 999669, "risk_tier": 2},
        })
        assert created.status_code in (200, 201), created.text
        result = client.post(f"/tasks/{task_id}/result", json={
            "task_id": task_id, "status": "failed", "summary": summary,
            "branch": BRANCH, "commit": SHA, "pr_number": 999669,
        })
        assert result.status_code == 200, result.text

    tasks = TaskQueue(tmp_db).list_tasks()
    assert not [t for t in tasks if t.task_id.startswith(f"retry-{task_id}-")]
    assert TaskQueue(tmp_db).get_task(task_id).context["retry_skipped_reason"] == (
        "work_pushed_or_unchanged_head")


def test_failed_implement_without_commit_still_retries(tmp_db, monkeypatch):
    from agent_crew import github

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setattr(github, "pr_head_sha", lambda *args, **kwargs: SHA)
    app = create_app(db_path=tmp_db, project="example", watchdog_disabled=True,
                     anomaly_disabled=True)
    with TestClient(app, headers={"X-Agent-Crew-Project": "example"}) as client:
        created = client.post("/tasks", json={
            "task_id": "impl-no-commit", "task_type": "implement",
            "description": "implement", "branch": BRANCH, "priority": 3,
            "project": "example", "context": {"repo": "owner/repo", "risk_tier": 2},
        })
        assert created.status_code in (200, 201), created.text
        result = client.post("/tasks/impl-no-commit/result", json={
            "task_id": "impl-no-commit", "status": "failed",
            "summary": "provider failed before push",
        })
        assert result.status_code == 200, result.text

    assert any(t.task_id == "retry-impl-no-commit-a1"
               for t in TaskQueue(tmp_db).list_tasks())


@pytest.mark.parametrize("remote_head,expect_retry", [(SHA, False), ("b" * 40, True)])
def test_branch_head_requires_the_reported_commit(tmp_db, monkeypatch,
                                                  remote_head, expect_retry):
    from agent_crew import github

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setattr(github, "branch_head_sha",
                        lambda branch, repo: remote_head)
    app = create_app(db_path=tmp_db, project="example", watchdog_disabled=True,
                     anomaly_disabled=True)
    with TestClient(app, headers={"X-Agent-Crew-Project": "example"}) as client:
        created = client.post("/tasks", json={
            "task_id": "impl-branch", "task_type": "implement",
            "description": "implement", "branch": BRANCH, "priority": 3,
            "project": "example", "context": {"repo": "owner/repo", "risk_tier": 2},
        })
        assert created.status_code in (200, 201), created.text
        result = client.post("/tasks/impl-branch/result", json={
            "task_id": "impl-branch", "status": "failed", "summary": "failed after push",
            "branch": BRANCH, "commit": SHA,
        })
        assert result.status_code == 200, result.text

    retries = [t for t in TaskQueue(tmp_db).list_tasks()
               if t.task_id.startswith("retry-impl-branch-")]
    assert bool(retries) is expect_retry
