"""A dispatched worktree can prove a missing implement result ref (#573)."""

import subprocess

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _worktree(tmp_path, head_state):
    remote = tmp_path / "remote.git"
    worker = tmp_path / "worker"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                   check=True, capture_output=True)
    subprocess.run(["git", "init", "--initial-branch=main", str(worker)],
                   check=True, capture_output=True)
    _git(worker, "config", "user.name", "Test User")
    _git(worker, "config", "user.email", "test@example.invalid")
    (worker / "base.txt").write_text("base\n")
    _git(worker, "add", "base.txt")
    _git(worker, "commit", "-m", "base")
    base = _git(worker, "rev-parse", "HEAD")
    _git(worker, "remote", "add", "origin", str(remote))
    _git(worker, "push", "origin", "main")
    if head_state in {"descendant", "unpublished", "unknown_path"}:
        _git(worker, "checkout", "-b", "feature")
        (worker / "change.txt").write_text("new work\n")
        _git(worker, "add", "change.txt")
        _git(worker, "commit", "-m", "feature")
        if head_state != "unpublished":
            _git(worker, "push", "origin", "feature")
    elif head_state == "unrelated":
        _git(worker, "checkout", "--orphan", "feature")
        _git(worker, "rm", "-rf", ".")
        (worker / "other.txt").write_text("unrelated\n")
        _git(worker, "add", "other.txt")
        _git(worker, "commit", "-m", "unrelated")
        _git(worker, "push", "origin", "feature")
    return worker, base, _git(worker, "rev-parse", "HEAD")


@pytest.mark.parametrize("head_state", [
    "descendant", "base", "unrelated", "unpublished", "unknown_path",
])
def test_missing_refs_use_only_descendant_dispatched_worktree_head(
        tmp_path, monkeypatch, head_state):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE__DEMO", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    worker, base, head = _worktree(tmp_path, head_state)
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest(
        "impl-573", "implement", "make a change", branch="main", project="demo",
        context={"worktree_base_sha": base, "base_branch": "main",
                 "crew_run_branch": False},
    ))
    if head_state != "unknown_path":
        queue.record_attribution("impl-573", worktree_path=str(worker),
                                 role="implementer", agent="codex")
    app = create_app(db, worktree_map={"implementer": str(worker)},
                     pane_map={}, identity_required=False,
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks/impl-573/result", json={
            "task_id": "impl-573", "status": "completed", "summary": "work done",
        })

    assert response.status_code == 200, response.text
    if head_state == "descendant":
        assert response.json().get("held") is None, response.text
        assert queue.get_task_status("impl-573") == "completed"
        result = queue.get_result("impl-573")
        assert (result.commit, result.branch) == (head, "feature")
        reviews = [task for task in queue.list_tasks() if task.task_type == "review"]
        assert len(reviews) == 1
        assert reviews[0].branch == "feature"
        assert reviews[0].context["reviewed_sha"] == head
    else:
        assert response.json()["held"] == "no_artifact"
        assert queue.get_task_status("impl-573") == "failed"
        if head_state == "unpublished":
            assert "not reachable from origin" in response.json()["detail"]
            assert queue.get_result("impl-573").commit == head
        else:
            assert queue.get_result("impl-573").commit == ""
