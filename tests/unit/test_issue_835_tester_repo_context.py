"""#835: a first-round script test keeps its repository identity and base."""

import subprocess

import pytest

from agent_crew import script_tester
from agent_crew import github
from agent_crew.pipeline import auto_enqueue_test
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


def test_first_round_test_inherits_repo_and_base_from_implement_ancestor(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(
        task_id="impl-835", task_type="implement", description="implement",
        branch="feature", context={"repo": "owner/project", "base_branch": "release"}))
    queue.enqueue(TaskRequest(
        task_id="review-835", task_type="review", description="review",
        branch="feature", context={"prev_task_id": "impl-835", "pr_number": 835}))
    queue.submit_result("review-835", TaskResult(
        task_id="review-835", status="completed", summary="approved",
        verdict="approve", findings=[], pr_number=835))

    test_id = auto_enqueue_test(queue, "review-835", pr_state_fn=lambda _: "open")

    assert test_id == "test-review-835"
    assert queue.get_task(test_id).context["repo"] == "owner/project"
    assert queue.get_task(test_id).context["base_branch"] == "release"


def test_pr_base_branch_always_passes_explicit_repo(monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="release\n")

    monkeypatch.setattr(script_tester.subprocess, "run", run)
    assert script_tester.pr_base_branch(835, "owner/project") == "release"
    assert commands == [["gh", "pr", "view", "835", "--json", "baseRefName",
                         "-q", ".baseRefName", "--repo", "owner/project"]]


def test_missing_repo_is_explicit_setup_error_without_gh(monkeypatch):
    monkeypatch.setattr(github, "get_repo", lambda cwd: None)
    monkeypatch.setattr(script_tester.subprocess, "run",
                        lambda *args, **kwargs: pytest.fail("gh was called"))
    task = TaskRequest(task_id="test-review-835", task_type="test",
                       description="test", branch="feature",
                       context={"pr_number": 835})
    result = script_tester.execute_test_task(
        task, repo="/local/checkout", state=None, cache_dir="/unused")
    assert result.status == "needs_human"
    assert "missing context.repo" in result.summary


def test_manual_test_task_derives_repo_from_its_project_checkout(tmp_path, monkeypatch):
    checkout = tmp_path / "project"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin",
                    "https://github.com/owner/project.git"], check=True)
    cwd_seen = []
    gh_repo_seen = []
    real_get_repo = github.get_repo
    monkeypatch.setattr(github, "get_repo",
                        lambda cwd: cwd_seen.append(cwd) or real_get_repo(cwd=cwd))
    monkeypatch.setattr(script_tester, "pr_base_branch",
                        lambda pr, repo: gh_repo_seen.append((pr, repo)) or "release")
    monkeypatch.setattr(script_tester, "resolve_refs", lambda *args, **kwargs: ("base", "head"))
    monkeypatch.setattr(script_tester, "run_script_test",
                        lambda *args, **kwargs: {"outcome": script_tester.PASSED,
                                                 "counts": {"passed": 1}})
    task = TaskRequest(task_id="manual-test-835", task_type="test",
                       description="manual test", branch="feature",
                       context={"pr_number": 835, "risk_tier": 1})

    result = script_tester.execute_test_task(
        task, repo=str(checkout), state=None, cache_dir="/unused")

    assert result.status == "completed"
    assert cwd_seen == [str(checkout)]
    assert gh_repo_seen == [(835, "owner/project")]


def test_known_base_branch_needs_no_gh_repo_lookup(monkeypatch):
    monkeypatch.setattr(script_tester, "pr_base_branch",
                        lambda *args, **kwargs: pytest.fail("gh lookup was called"))
    monkeypatch.setattr(script_tester, "resolve_refs", lambda *args, **kwargs: ("base", "head"))
    monkeypatch.setattr(script_tester, "run_script_test",
                        lambda *args, **kwargs: {"outcome": script_tester.PASSED,
                                                 "counts": {"passed": 1}})
    task = TaskRequest(task_id="known-base-835", task_type="test",
                       description="known base", branch="feature",
                       context={"pr_number": 835, "base_branch": "release"})

    result = script_tester.execute_test_task(
        task, repo="/project/worktree", state=None, cache_dir="/unused")

    assert result.status == "completed"
