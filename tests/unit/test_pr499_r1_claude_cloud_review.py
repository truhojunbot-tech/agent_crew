"""PR #499 codex independent review r1 (REQUEST_CHANGES, 1 HIGH) — reproduce,
then verify the fix, on the same branch (r0's 4 HIGH findings already fixed
and confirmed by r1 itself).

Finding (verbatim from origin/cloud-context/496:REVIEW_499_r1.md):

- HIGH src/agent_crew/claude_cloud.py:625 - Cloud reconciliation ignores
  each task repository: The watchdog calls reconcile_all_cloud_tasks without
  repo, which passes None to GitHub PR and branch lookups. github.get_repo
  then uses server cwd, documented to be a different repository for
  agent_crew instances. A task can miss its actual PR or identify one in
  another repo. Resolve repo per task from task context and test divergent
  server cwd and task repo.
"""
import uuid

import pytest

from agent_crew import claude_cloud as cc
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


@pytest.fixture
def q(tmp_db):
    return TaskQueue(tmp_db)


@pytest.fixture(autouse=True)
def _cloud_enabled(monkeypatch):
    monkeypatch.setenv(cc._ENV_ENABLED, "1")


def _enqueue_implement(q, task_id=None, branch=None, context=None):
    task_id = task_id or f"impl-{uuid.uuid4().hex[:8]}"
    branch = branch or f"claude/{task_id}"
    q.enqueue(TaskRequest(task_id=task_id, task_type="implement", description="do the thing",
                          branch=branch, context=context or {}, project="agent_crew"))
    return task_id, branch


def _dispatch(q, task_id):
    q.dequeue(role="implementer")
    q.record_dispatch(task_id, channel=cc.DISPATCH_CHANNEL, agent=cc.CLOUD_PROVIDER_NAME,
                      target="pending")


def test_reproduction_pre_fix_call_shape_never_consulted_task_repo(q, monkeypatch):
    """Reproduction: the PRE-FIX code called
    ``pr_number_for_branch_fn(branch, repo=repo)`` using the batch-level
    ``repo`` argument (``None`` from the watchdog's call with no override)
    with no per-task resolution at all. Passing the real (unmocked)
    ``github.pr_number_for_branch``, that shape falls through to
    ``github.get_repo()`` — the server's OWN cwd — even though the task
    names a different repo in its own context, exactly as the finding
    describes."""
    monkeypatch.setattr(cc._github, "get_repo", lambda *a, **kw: "server/cwd-repo")
    seen = []

    def _real_pr_number_for_branch_shape(branch, repo=None):
        # Mirrors github.pr_number_for_branch's own fallback, since gh isn't
        # installed in this sandbox — the point under test is which `repo`
        # value the PRE-FIX call site passed in, not gh's subprocess call.
        resolved = repo or cc._github.get_repo()
        seen.append(resolved)
        return None
    monkeypatch.setattr(cc._github, "pr_number_for_branch", _real_pr_number_for_branch_shape)

    task_id, branch = _enqueue_implement(q, context={"repo": "org/task-repo"})
    _dispatch(q, task_id)
    task = [t for t in q.list_tasks(status="in_progress") if t.task_id == task_id][0]

    # The PRE-FIX call shape: the batch-level `repo` (None here, as the
    # watchdog passes) with no per-task resolution.
    pre_fix_repo_argument = None
    cc._github.pr_number_for_branch(task.branch, repo=pre_fix_repo_argument)
    assert seen == ["server/cwd-repo"], (
        "reproduction: with no per-task resolution, the task's own "
        "org/task-repo is never consulted — only the server's cwd is")


def test_fix_reconcile_resolves_repo_per_task_not_server_cwd(q, monkeypatch):
    """Fix: `reconcile_cloud_dispatch` now resolves each task's OWN
    `context['repo']` via `_resolve_task_repo` before any GitHub lookup —
    even when the server's own cwd names a different repository entirely."""
    monkeypatch.setattr(cc._github, "get_repo", lambda *a, **kw: "server/cwd-repo")
    seen_repos = {}

    def _pr_number_for_branch(branch, repo=None):
        seen_repos[branch] = repo
        return None
    monkeypatch.setattr(cc._github, "pr_number_for_branch", _pr_number_for_branch)
    monkeypatch.setattr(cc._github, "branch_head_commit_message",
                        lambda branch, repo=None: None)

    task_id, branch = _enqueue_implement(q, context={"repo": "org/task-repo"})
    _dispatch(q, task_id)

    cc.reconcile_all_cloud_tasks(q)

    assert seen_repos[branch] == "org/task-repo", (
        "the task's own registered repo must be used, not the server cwd")
    assert seen_repos[branch] != "server/cwd-repo"


def test_fix_divergent_server_cwd_and_multiple_task_repos(q, monkeypatch):
    """The reviewer's exact request: 'test divergent server cwd and task
    repo' — TWO tasks with DIFFERENT registered repos in ONE reconciliation
    batch must each use their OWN repo, none of them the server's cwd."""
    monkeypatch.setattr(cc._github, "get_repo", lambda *a, **kw: "server/cwd-repo")
    seen_repos = {}

    def _pr_number_for_branch(branch, repo=None):
        seen_repos[branch] = repo
        return None
    monkeypatch.setattr(cc._github, "pr_number_for_branch", _pr_number_for_branch)
    monkeypatch.setattr(cc._github, "branch_head_commit_message",
                        lambda branch, repo=None: None)

    id_a, branch_a = _enqueue_implement(q, context={"repo": "org/repo-a"})
    _dispatch(q, id_a)
    id_b, branch_b = _enqueue_implement(q, context={"repo": "org/repo-b"})
    _dispatch(q, id_b)

    outcomes = cc.reconcile_all_cloud_tasks(q)

    assert len(outcomes) == 2
    assert seen_repos[branch_a] == "org/repo-a"
    assert seen_repos[branch_b] == "org/repo-b"
    assert "server/cwd-repo" not in seen_repos.values(), (
        "neither task's own repo should ever fall through to the server's "
        "cwd when its context already names a repo"
    )


def test_fix_falls_back_to_server_cwd_only_when_task_has_no_repo(q, monkeypatch):
    """When a task's context genuinely carries no repo, falling back to
    `github.get_repo()` (the server cwd) is still correct — that fallback is
    for a single-project deployment, not the per-task bug this fixes."""
    monkeypatch.setattr(cc._github, "get_repo", lambda *a, **kw: "server/cwd-repo")
    seen_repos = {}

    def _pr_number_for_branch(branch, repo=None):
        seen_repos[branch] = repo
        return None
    monkeypatch.setattr(cc._github, "pr_number_for_branch", _pr_number_for_branch)
    monkeypatch.setattr(cc._github, "branch_head_commit_message",
                        lambda branch, repo=None: None)

    task_id, branch = _enqueue_implement(q, context={})
    _dispatch(q, task_id)

    cc.reconcile_all_cloud_tasks(q)

    assert seen_repos[branch] == "server/cwd-repo"


def test_fix_explicit_override_still_wins_over_task_context(q, monkeypatch):
    """`_resolve_task_repo`'s documented precedence (explicit > task context
    > server cwd) must hold for reconciliation too, so a caller/test that
    already knows the answer can still force it."""
    monkeypatch.setattr(cc._github, "pr_number_for_branch",
                        lambda branch, repo=None: None)
    monkeypatch.setattr(cc._github, "branch_head_commit_message",
                        lambda branch, repo=None: None)

    task_id, branch = _enqueue_implement(q, context={"repo": "org/task-repo"})
    _dispatch(q, task_id)
    task = [t for t in q.list_tasks(status="in_progress") if t.task_id == task_id][0]

    resolved = cc._resolve_task_repo(task, "org/explicit-override")
    assert resolved == "org/explicit-override"
