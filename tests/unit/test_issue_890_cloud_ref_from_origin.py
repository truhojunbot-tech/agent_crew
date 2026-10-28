"""#890: cloud implements derive a task branch and resolve their ref from origin."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


SESSION = "session_890abcde"


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def _checkout(tmp_path):
    origin = "https://github.com/owner/repo.git"
    remote = tmp_path / "remote.git"
    _git("init", "--bare", str(remote))
    checkout = tmp_path / "worktrees" / "agent_crew" / "claude"
    checkout.mkdir(parents=True)
    _git("init", "-b", "main", cwd=checkout)
    _git("config", "user.email", "test@example.com", cwd=checkout)
    _git("config", "user.name", "Test", cwd=checkout)
    (checkout / "README.md").write_text("fixture\n")
    _git("add", "README.md", cwd=checkout)
    _git("commit", "-m", "fixture", cwd=checkout)
    _git("remote", "add", "origin", origin, cwd=checkout)
    _git("config", f"url.file://{remote}/.insteadOf", origin, cwd=checkout)
    _git("push", "origin", "HEAD:refs/heads/main", cwd=checkout)
    _git("fetch", "origin", cwd=checkout)
    return checkout


def _leave_on_unpublished_local_branch(checkout):
    """What a local claude implementer leaves behind (#890, 13:35Z)."""
    _git("checkout", "-b", "agent/405-release-scope-local", cwd=checkout)
    (checkout / "README.md").write_text("local only\n")
    _git("commit", "-am", "local only", cwd=checkout)
    return _git("rev-parse", "HEAD", cwd=checkout)


def _state(tmp_path, checkout):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"project": "agent_crew",
                                "worktrees": {"claude": str(checkout)}}))
    return str(path)


def _run(launches):
    def run(argv, **kwargs):
        if "--help" in argv:
            return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
        launches.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "session_id": SESSION, "url": f"https://claude.ai/code/{SESSION}"}))
    return run


def _enqueue(queue, *, task_id="impl-890", branch="main", context=None):
    queue.enqueue(TaskRequest(task_id=task_id, task_type="implement",
                              description="Implement #890", branch=branch,
                              project="agent_crew",
                              context={"repo": "owner/repo", "base_branch": "main",
                                       **(context or {})}))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setattr(cloud, "read_cloud_credit_used", lambda: None)
    monkeypatch.setattr(cloud, "_resolve_resume_session_id", lambda queue, task: None)
    monkeypatch.setattr("agent_crew.pipeline.auto_fallback_failed_task",
                        lambda *a, **k: None)


@pytest.mark.parametrize("branch", ["main", "", " ", "master"])
def test_base_branch_implement_gets_derived_task_branch(tmp_path, env, monkeypatch, branch):
    monkeypatch.setattr(cloud, "_validated_cloud_checkout",
                        lambda project, repo, state_path=None: ("/fixture/claude", repo))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _enqueue(queue, branch=branch)
    launches = []
    outcome = cloud.dispatch_cloud_for_role(queue, role="implementer",
                                            task_type="implement", run_fn=_run(launches))
    assert outcome.dispatched, outcome
    derived = "agent/agent_crew/impl-890"
    assert queue.get_task("impl-890").branch == derived
    assert f"Push your work to branch: {derived}" in launches[0][0][2]
    assert queue.get_task_context("impl-890")["cloud_derived_branch"] == derived


def test_worktree_on_unpublished_local_branch_does_not_fail_launch(tmp_path, env):
    checkout = _checkout(tmp_path)
    base = _git("rev-parse", "HEAD", cwd=checkout)
    local_head = _leave_on_unpublished_local_branch(checkout)
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _enqueue(queue, branch="main", context={"worktree_base_sha": base})
    launches = []
    outcome = cloud.dispatch_cloud_for_role(queue, role="implementer", task_type="implement",
                                            state_path=_state(tmp_path, checkout),
                                            run_fn=_run(launches))
    assert outcome.dispatched, outcome
    assert launches[0][1]["cwd"] == str(checkout)
    assert f"--ref {base}" in launches[0][0][2]
    assert local_head not in launches[0][0][2]


def test_lineage_implement_resolves_ref_from_origin_not_local_head(tmp_path, env):
    checkout = _checkout(tmp_path)
    _git("push", "origin", "HEAD:refs/heads/fix/890-pushed", cwd=checkout)
    local_head = _leave_on_unpublished_local_branch(checkout)
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _enqueue(queue, branch="fix/890-pushed",
             context={"prev_task_id": "impl-prior", "fix_round": 0})
    launches = []
    outcome = cloud.dispatch_cloud_for_role(queue, role="implementer", task_type="implement",
                                            state_path=_state(tmp_path, checkout),
                                            run_fn=_run(launches))
    assert outcome.dispatched, outcome
    assert "--ref fix/890-pushed" in launches[0][0][2]
    assert local_head not in launches[0][0][2]


def test_lineage_implement_with_unpushed_branch_uses_origin_default(tmp_path, env):
    checkout = _checkout(tmp_path)
    _leave_on_unpublished_local_branch(checkout)
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _enqueue(queue, branch="fix/890-not-pushed",
             context={"prev_task_id": "impl-prior", "fix_round": 0})
    launches = []
    outcome = cloud.dispatch_cloud_for_role(queue, role="implementer", task_type="implement",
                                            state_path=_state(tmp_path, checkout),
                                            run_fn=_run(launches))
    assert outcome.dispatched, outcome
    assert "--ref main" in launches[0][0][2]


def test_review_still_requires_published_checkout_head(tmp_path):
    checkout = _checkout(tmp_path)
    _leave_on_unpublished_local_branch(checkout)
    state_path = _state(tmp_path, checkout)
    assert cloud._validated_cloud_checkout("agent_crew", "owner/repo", state_path) == (
        str(checkout), "owner/repo")
    assert not cloud._checkout_head_published(str(checkout))


@pytest.mark.parametrize("branch,context", [
    ("bad..ref", {}),
    ("has space", {}),
    ("trailing/", {}),
    # A continuing lineage cannot be moved to a derived branch: its PR lives
    # on the named branch.
    ("main", {"prev_task_id": "impl-prior", "fix_round": 0}),
])
def test_explicit_unpublishable_ref_still_fails_closed(tmp_path, env, branch, context):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _enqueue(queue, branch=branch, context=context)
    launches = []
    outcome = cloud.dispatch_cloud_for_role(queue, role="implementer",
                                            task_type="implement", run_fn=_run(launches))
    assert outcome.launch_error == "cloud_branch_unpublishable"
    assert queue.get_task("impl-890").status == "failed"
    assert launches == []
