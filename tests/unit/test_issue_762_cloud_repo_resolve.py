"""#762: cloud implement tasks resolve their repo from the crew checkout."""

import json
import subprocess
from types import SimpleNamespace

from click.testing import CliRunner

from agent_crew import claude_cloud as cloud
from agent_crew.cli import crew
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


def _git_repo(path, origin):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return path


def _state(tmp_path, claude_checkout):
    # Deliberately no repo_path: crews set up before it existed lack it.
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"project": "agent_crew",
                                "worktrees": {"claude": str(claude_checkout)}}))
    return str(path)


def _implement(task_id="impl-762", context=None):
    return TaskRequest(task_id=task_id, task_type="implement", description="Implement #762",
                       branch=f"agent/{task_id}", project="agent_crew", context=context or {})


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
