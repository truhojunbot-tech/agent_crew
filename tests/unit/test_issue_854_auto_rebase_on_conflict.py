"""#854: a script-test squash conflict gets one reviewed rebase round."""

import sqlite3
import subprocess

import pytest
from fastapi.testclient import TestClient

from agent_crew.pipeline import verify_rebase_artifact
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


PR = 854
HEAD_1 = "a" * 40
HEAD_2 = "b" * 40


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    import agent_crew.github as github
    monkeypatch.setattr(github, "pr_state", lambda *args, **kwargs: "open")
    head = {"sha": HEAD_1}
    monkeypatch.setattr(github, "pr_head_sha", lambda *args, **kwargs: head["sha"])
    db = str(tmp_path / "tasks.db")
    notices = []
    app = create_app(db, project="agent_crew", pane_map={},
                     watchdog_disabled=True, anomaly_disabled=True,
                     identity_required=False,
                     coordinator_notify_fn=lambda message: notices.append(message) or True)
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest(
        task_id="test-original", task_type="test", description="Test PR",
        branch="fix/854-conflict", project="agent_crew",
        context={"pr_number": PR, "reviewed_sha": HEAD_1,
                 "base_branch": "main", "repo": "owner/repo", "issue": 854,
                 "risk_tier": 2, "implementer_agent": "codex",
                 "reviewer_agent": "claude"}), ingress="http.tasks")
    with TestClient(app) as client:
        yield client, queue, notices, head


def _conflict(client, task_id="test-original", head=HEAD_1, reason="merge_conflict"):
    return client.post(f"/tasks/{task_id}/result", json={
        "task_id": task_id, "status": "failed",
        "summary": f"script test FAILED ({reason})", "pr_number": PR,
        "error_info": {"reason": f"script_test_{reason}", "final": True,
                       "script_test": {"outcome": "FAILED", "reason": reason,
                                       "head_sha": head}},
    })


def _rebase_rounds(queue):
    return [task for task in queue.list_tasks()
            if task.task_type == "implement"
            and isinstance(task.context, dict)
            and task.context.get("auto_rebase_of")]


def test_conflict_enqueues_one_rebase_round_without_failure_notice(harness):
    client, queue, notices, _ = harness

    response = _conflict(client)

    assert response.status_code == 200, response.text
    rounds = _rebase_rounds(queue)
    assert len(rounds) == 1
    task = rounds[0]
    assert task.branch == "fix/854-conflict"
    assert task.context["pr_number"] == PR
    assert task.context["prev_task_id"] == "test-original"
    assert task.context["auto_rebase_of"] == "test-original"
    assert task.context["artifact_kind"] == "rebase"
    assert task.context["rebase_onto"] == "main"
    assert task.context["rebase_source_sha"] == HEAD_1
    assert task.context["issue"] == 854
    assert task.context["repo"] == "owner/repo"
    assert task.context["risk_tier"] == 2
    assert "origin/main" in task.description
    assert "force-push" in task.description
    assert notices == []

    replay = _conflict(client)
    assert replay.status_code == 200, replay.text
    assert len(_rebase_rounds(queue)) == 1
    assert notices == []


@pytest.mark.parametrize("pr_state", ["merged", "closed", None])
def test_terminal_or_unknown_pr_does_not_enqueue_rebase(harness, monkeypatch, pr_state):
    import agent_crew.github as github

    client, queue, notices, _ = harness
    monkeypatch.setattr(github, "pr_state", lambda *args, **kwargs: pr_state)

    response = _conflict(client)

    assert response.status_code == 200, response.text
    assert _rebase_rounds(queue) == []
    assert len(notices) == 1 and "failed_no_successor" in notices[0]


@pytest.mark.parametrize("current_head", [HEAD_2, ""])
def test_moved_or_unreadable_pr_head_does_not_enqueue_rebase(harness, current_head):
    client, queue, notices, head = harness
    head["sha"] = current_head

    response = _conflict(client)

    assert response.status_code == 200, response.text
    assert _rebase_rounds(queue) == []
    assert len(notices) == 1 and "failed_no_successor" in notices[0]


def test_second_conflict_on_rebase_lineage_notifies_without_new_round(harness):
    client, queue, notices, head = harness
    assert _conflict(client).status_code == 200
    rebase_id = _rebase_rounds(queue)[0].task_id
    queue.enqueue(TaskRequest(
        task_id="review-rebase", task_type="review", description="review rebased PR",
        branch="fix/854-conflict", project="agent_crew",
        context={"prev_task_id": rebase_id, "pr_number": PR,
                 "repo": "owner/repo", "issue": 854, "risk_tier": 2,
                 "reviewed_sha": HEAD_2}), ingress="http.tasks")
    with sqlite3.connect(queue.db_path) as conn:
        conn.execute("UPDATE tasks SET status='completed' WHERE task_id='review-rebase'")
    queue.enqueue(TaskRequest(
        task_id="test-second", task_type="test", description="test rebased PR",
        branch="fix/854-conflict", project="agent_crew",
        context={"prev_task_id": "review-rebase", "pr_number": PR,
                 "repo": "owner/repo", "issue": 854, "risk_tier": 2,
                 "reviewed_sha": HEAD_2}), ingress="http.tasks")
    head["sha"] = HEAD_2

    response = _conflict(client, "test-second", HEAD_2)

    assert response.status_code == 200, response.text
    assert len(_rebase_rounds(queue)) == 1
    assert len(notices) == 1
    assert "failed_no_successor" in notices[0]


def test_kill_switch_preserves_old_terminal_notice(harness, monkeypatch):
    client, queue, notices, _ = harness
    monkeypatch.setenv("AGENT_CREW_AUTO_REBASE_ON_CONFLICT", "0")

    response = _conflict(client)

    assert response.status_code == 200, response.text
    assert _rebase_rounds(queue) == []
    assert len(notices) == 1 and "failed_no_successor" in notices[0]


def test_other_not_run_reason_is_unchanged(harness):
    client, queue, notices, _ = harness

    response = _conflict(client, reason="exit_code_2")

    assert response.status_code == 200, response.text
    assert _rebase_rounds(queue) == []
    assert len(notices) == 1 and "failed_no_successor" in notices[0]


def test_rebase_round_commit_passes_existing_artifact_verifier(tmp_path):
    def git(cwd, *args):
        return subprocess.run(
            ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
             *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()

    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    git(work, "checkout", "-q", "-b", "main")
    (work / "base.txt").write_text("base\n")
    git(work, "add", "base.txt")
    git(work, "commit", "-q", "-m", "base")
    git(work, "push", "-q", "origin", "main")
    git(work, "checkout", "-q", "-b", "fix/854-conflict")
    (work / "feature.txt").write_text("feature\n")
    git(work, "add", "feature.txt")
    git(work, "commit", "-q", "-m", "feature")
    source = git(work, "rev-parse", "HEAD")
    git(work, "push", "-q", "origin", "fix/854-conflict")
    verifier = tmp_path / "verifier"
    git(tmp_path, "clone", "-q", str(origin), str(verifier))
    git(work, "checkout", "-q", "main")
    (work / "main.txt").write_text("main moved\n")
    git(work, "add", "main.txt")
    git(work, "commit", "-q", "-m", "main moved")
    git(work, "push", "-q", "origin", "main")
    git(work, "checkout", "-q", "fix/854-conflict")
    git(work, "rebase", "-q", "main")
    commit = git(work, "rev-parse", "HEAD")
    git(work, "push", "-q", "--force", "origin", "fix/854-conflict")
    task = TaskRequest(
        task_id="rebase-pr854", task_type="implement", description="rebase",
        branch="fix/854-conflict", context={"rebase_onto": "main",
                                            "rebase_source_sha": source})
    result = TaskResult(task_id=task.task_id, status="completed", summary="rebased",
                        branch=task.branch, commit=commit)

    passed, detail = verify_rebase_artifact(task, result, repo_cwd=str(verifier))

    assert passed, detail
    assert "origin/main" in detail
