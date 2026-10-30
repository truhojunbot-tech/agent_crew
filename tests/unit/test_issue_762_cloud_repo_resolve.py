"""#762: cloud implement tasks resolve their repo from the crew checkout."""

import json
import sqlite3
import subprocess
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from click.testing import CliRunner

from agent_crew import claude_cloud as cloud
from agent_crew.cli import crew
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _git_repo(path, origin):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return path


def _state(tmp_path, claude_checkout):
    # Deliberately no repo_path: crews set up before it existed lack it.
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"project": "agent_crew",
                                "worktrees": {"claude": str(claude_checkout)},
                                # #891: the claim checks cloud holds the role it dispatches.
                                "role_agents": {"implementer": "claude_cloud", "reviewer": "codex",
                                                "tester": "script"}}))
    return str(path)


def _implement(task_id="impl-762", context=None):
    return TaskRequest(task_id=task_id, task_type="implement", description="Implement #762",
                       branch=f"agent/{task_id}", project="agent_crew",
                       context={"risk_tier": 1, **(context or {})})


def _cloud_env(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_CONCURRENCY", "1")
    monkeypatch.delenv("AGENT_CREW_STATE", raising=False)


def _run(argv, **kwargs):
    if "--help" in argv:
        return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
    return SimpleNamespace(returncode=0, stdout=json.dumps({
        "session_id": "session_abc12345", "url": "https://claude.ai/code/session_abc12345"}))


def test_implement_resolves_claude_worktree_origin_not_server_cwd(tmp_path, monkeypatch):
    checkout = _git_repo(tmp_path / "claude", "https://github.com/owner/agent_crew.git")
    server_cwd = _git_repo(tmp_path / "server", "https://github.com/owner/alfred.git")
    monkeypatch.chdir(server_cwd)
    monkeypatch.delenv("AGENT_CREW_STATE", raising=False)

    assert cloud._resolve_task_repo(_implement(), None, _state(tmp_path, checkout)) == \
        "owner/agent_crew"


def test_implement_never_falls_back_to_server_cwd(tmp_path, monkeypatch):
    server_cwd = _git_repo(tmp_path / "server", "https://github.com/owner/alfred.git")
    monkeypatch.chdir(server_cwd)
    _cloud_env(monkeypatch)
    state = _state(tmp_path, tmp_path / "missing")
    assert cloud._resolve_task_repo(_implement(), None, state) == ""

    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(_implement())
    outcome = cloud.dispatch_cloud_for_role(queue, role="implementer", task_type="implement",
                                            run_fn=_run, state_path=state)
    assert outcome.launch_error == "cloud_repo_unresolved"
    assert queue.get_task("impl-762").status == "failed"


def test_crew_run_repo_sets_implement_context(monkeypatch, tmp_path):
    db = tmp_path / "tasks.db"
    monkeypatch.setattr(TaskQueue, "get_result", lambda _queue, task_id: TaskResult(
        task_id=task_id, status="failed", summary="fixture stops after enqueue"))
    result = CliRunner().invoke(crew, ["run", "implement #762", "--db", str(db),
                                       "--branch", "fix/762", "--no-tester",
                                       "--issue", "762", "--repo", "owner/agent_crew"])
    assert result.exit_code == 0, result.output
    tasks = TaskQueue(str(db)).list_tasks()
    assert [t.context.get("repo") for t in tasks] == ["owner/agent_crew"]


def test_cloud_implement_without_context_repo_reconciles_and_enqueues_review(
        tmp_path, monkeypatch):
    checkout = _git_repo(tmp_path / "claude", "https://github.com/owner/agent_crew.git")
    server_cwd = _git_repo(tmp_path / "server", "https://github.com/owner/alfred.git")
    monkeypatch.chdir(server_cwd)
    _cloud_env(monkeypatch)
    monkeypatch.setattr(cloud, "_validated_cloud_checkout",
                        lambda project, repo, state_path=None: (str(checkout), repo))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(_implement())

    assert cloud.dispatch_cloud_for_role(queue, role="implementer", task_type="implement",
                                         run_fn=_run,
                                         state_path=_state(tmp_path, checkout)).dispatched
    assert queue.get_task_context("impl-762")["repo"] == "owner/agent_crew"

    looked_up = []

    def pr_for_branch(branch, repo=None):
        looked_up.append(repo)
        return 763

    outcome = cloud.reconcile_cloud_dispatch(
        queue, queue.get_task("impl-762"), pr_number_for_branch_fn=pr_for_branch,
        pr_head_sha_fn=lambda *a, **kw: "a" * 40,
        commit_message_fn=lambda *a, **kw: None,
        pr_state_fn=lambda *a, **kw: "open")
    assert outcome.action == "pr_ready"
    assert looked_up == ["owner/agent_crew"]
    assert queue.get_task("impl-762").status == "completed"
    reviews = [t for t in queue.list_tasks() if t.task_type == "review"]
    assert len(reviews) == 1


def test_default_pr_state_lookup_is_bound_to_context_repo(tmp_path, monkeypatch):
    """#762 r1 HIGH: no injected pr_state_fn — the DEFAULT lookup must get the repo."""
    from agent_crew import github

    server_cwd = _git_repo(tmp_path / "server", "https://github.com/owner/wrong.git")
    monkeypatch.chdir(server_cwd)
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(_implement(context={"repo": "owner/right"}))
    queue.dequeue("implementer")

    seen = []

    def fake_pr_state(pr_number, repo=None, timeout=20.0, cwd=None):
        seen.append((pr_number, repo))
        return "open" if repo == "owner/right" else "unknown"

    monkeypatch.setattr(github, "pr_state", fake_pr_state)

    outcome = cloud.reconcile_cloud_dispatch(
        queue, queue.get_task("impl-762"),
        pr_number_for_branch_fn=lambda branch, repo=None: 763,
        pr_head_sha_fn=lambda *a, **kw: "a" * 40,
        commit_message_fn=lambda *a, **kw: None)

    assert seen and all(repo == "owner/right" for _, repo in seen), seen
    assert outcome.action == "pr_ready"
    assert queue.get_task("impl-762").status == "completed"
    reviews = [t for t in queue.list_tasks() if t.task_type == "review"]
    assert len(reviews) == 1


# #890: branch=main / missing now gets a derived task branch; only an explicit
# ref that cannot be published still fails closed.
@pytest.mark.parametrize("branch", ["bad..ref", "has space"])
def test_cloud_implement_refuses_unpublishable_branch_before_launch(tmp_path, monkeypatch,
                                                                    branch):
    _cloud_env(monkeypatch)
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("impl-branch", "implement", "Implement #762",
                              branch=branch, project="agent_crew",
                              context={"repo": "owner/agent_crew", "base_branch": "main"}))
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return _run(argv, **kwargs)
    outcome = cloud.dispatch_cloud_for_role(queue, role="implementer", task_type="implement",
                                            run_fn=run)
    assert outcome.launch_error == "cloud_branch_unpublishable"
    assert queue.get_task("impl-branch").status == "failed"
    assert calls == [["claude", "--help"]]


@pytest.mark.parametrize("description", [
    "Read ~/alfred/governance/plan.md before editing",
    "Inspect ~/.agent_crew/state.json for this task",
    "Use /tmp/worktrees/agent_crew/codex/local.txt",
])
def test_cloud_preflight_refuses_declared_local_inputs(tmp_path, monkeypatch, description):
    _cloud_env(monkeypatch)
    monkeypatch.setattr(cloud, "_validated_cloud_checkout",
                        lambda *a, **kw: ("/fixture/claude", "owner/agent_crew"))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("impl-local", "implement", description,
                              branch="agent/762-work", project="agent_crew",
                              context={"repo": "owner/agent_crew",
                                       "local_inputs": [description]}))
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return _run(argv, **kwargs)
    outcome = cloud.dispatch_cloud_for_role(queue, role="implementer", task_type="implement",
                                            run_fn=run)
    assert outcome.launch_error == "cloud_local_files_required"
    assert queue.get_task("impl-local").status == "failed"
    assert calls == [["claude", "--help"]]


def test_cloud_cost_recorded_on_result_cancel_and_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "0")
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    for task_id in ("posted", "cancelled", "failed"):
        queue.enqueue(_implement(task_id, {"repo": "owner/agent_crew",
                                           "cloud_session_id": "session_abc12345",
                                           "cloud_attempt_started_at": time.time() - 20,
                                           "cloud_credit_used_before": 4.0}))
        queue.record_attribution(task_id=task_id, project="agent_crew",
                                 agent="claude_cloud", role="implementer",
                                 task_type="implement", provider_session_id="session_abc12345",
                                 started_at=time.time())
    with sqlite3.connect(queue._db_path) as db:
        db.execute("UPDATE tasks SET status='in_progress', claim_source='cloud_push', "
                   "dispatch_channel='claude_cloud' WHERE task_id IN "
                   "('posted', 'cancelled', 'failed')")
    monkeypatch.setattr(cloud, "read_cloud_credit_used", lambda: 4.5)
    app = create_app(queue._db_path, pane_map={}, watchdog_disabled=True,
                     anomaly_disabled=True, identity_required=False)
    with TestClient(app) as client:
        assert client.post("/tasks/posted/result", json={
            "task_id": "posted", "status": "completed", "summary": "done"}).status_code == 200
        assert client.delete("/tasks/cancelled").status_code == 200
        assert app.state.fail_if_active("failed", "test_failure")
    assert [queue.get_attribution(task_id)["cloud_cost_status"] for task_id in
            ("posted", "cancelled", "failed")] == ["PENDING_SETTLE"] * 3
    assert [queue.get_attribution(task_id)["cloud_usd"] for task_id in
            ("posted", "cancelled", "failed")] == [None] * 3
    day_start = int(time.time() // 86400) * 86400
    assert queue.cloud_implement_usd_since(day_start) == 0.0
    monkeypatch.setenv("AGENT_CREW_CLOUD_COST_SETTLE_SECONDS", "0")
    cloud.reconcile_all_cloud_tasks(queue)
    assert [queue.get_attribution(task_id)["cloud_cost_status"] for task_id in
            ("posted", "cancelled", "failed")] == ["UNKNOWN_OVERLAP"] * 3
    monkeypatch.setattr(cloud, "read_cloud_credit_used", lambda: 9.0)
    cloud.record_terminal_cloud_cost(queue, "posted")
    assert queue.get_attribution("posted")["cloud_usd"] is None
