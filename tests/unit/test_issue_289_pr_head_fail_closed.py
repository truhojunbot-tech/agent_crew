"""#289 — a PR task that cannot find its PR must not review main instead.

#287 pinned reviewer/tester to an immutable commit once a target resolves. It
left the path *before* that intact: when `gh pr view <pr> --json headRefName`
fails, prep fell back to `task.branch`, which for a normal PR task is the base
branch — usually `main`.

    PR task → head lookup fails → pr_branch = main → detach at main
            → reviewed_sha = main's SHA → HEAD == reviewed_sha ✓

Every identity guard is satisfied and the wrong artifact is reviewed. I wrote
the log line for this myself — "THIS MAY NOT BE THE PR'S CODE; treat any finding
from this task as suspect" — which says plainly that logging was never the fix.
#250/#251 is the same class with production precedent.

⛔The asymmetry decides the behaviour: a deferred review is recoverable, a
  confident review of the wrong tree is not — it is indistinguishable from a
  real one, and its cost is attributed to a valid SHA under the wrong treatment.
  So this fails closed: no checkout, no provider launch, no economics row.
"""

import asyncio
import json
import os
import subprocess

import pytest

from agent_crew import server as sv
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue

PR_BRANCH = "fix/pr-head-289"
PR_NUMBER = 289


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=60)


def _sha(repo, ref="HEAD"):
    return _git("rev-parse", ref, cwd=repo).stdout.strip()


@pytest.fixture
def pr_repo(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   capture_output=True, check=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(origin), str(clone)], capture_output=True,
                   check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        _git("config", k, v, cwd=clone)
    (clone / "a.txt").write_text("base\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-m", "base", cwd=clone)
    _git("push", "-u", "origin", "main", cwd=clone)

    # ⛔The worktree starts at the BASE commit, before either the PR branch or
    #   main moves on. Creating it later made "unmoved" and "at main's tip" the
    #   same SHA, so the assertion that the fallback did not happen could not
    #   fail — the first version of this fixture proved nothing.
    wt = tmp_path / "wt"
    _git("worktree", "add", "--detach", str(wt), "HEAD", cwd=clone)

    _git("checkout", "-b", PR_BRANCH, cwd=clone)
    (clone / "a.txt").write_text("the PR's code\n")
    _git("commit", "-am", "A", cwd=clone)
    _git("push", "-u", "origin", PR_BRANCH, cwd=clone)
    sha_a = _sha(clone)
    _git("checkout", "main", cwd=clone)

    (clone / "b.txt").write_text("main moved\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-m", "main moves", cwd=clone)
    _git("push", "origin", "main", cwd=clone)
    main_tip = _sha(clone)

    assert len({_sha(wt), sha_a, main_tip}) == 3, "fixture is degenerate"
    return clone, wt, sha_a, main_tip


# ── 1. prep refuses rather than guessing ──────────────────────────────


@pytest.mark.parametrize("role", ["reviewer", "tester"])
def test_an_unresolvable_pr_head_is_not_silently_main(pr_repo, monkeypatch, role):
    """★★The bug. `task.branch` is the BASE branch for a PR task, so the
    fallback reviews main and calls it the PR."""
    clone, wt, sha_a, main_tip = pr_repo
    before = _sha(wt)
    monkeypatch.setattr(sv, "_resolve_pr_head_branch", lambda *a, **k: None)

    with pytest.raises(sv.WorktreeTargetUnresolved):
        sv._prepare_worktree_for_task(
            str(wt), "task-289", "main", role, task_context={"pr_number": PR_NUMBER})
    assert _sha(wt) == before, "the worktree was moved despite an unresolved target"
    assert _sha(wt) != main_tip


@pytest.mark.parametrize("role", ["reviewer", "tester"])
def test_a_resolvable_pr_head_still_pins(pr_repo, monkeypatch, role):
    """⛔The control: failing closed must not break the normal path."""
    clone, wt, sha_a, _ = pr_repo
    monkeypatch.setattr(sv, "_resolve_pr_head_branch", lambda *a, **k: PR_BRANCH)
    reviewed = sv._prepare_worktree_for_task(
        str(wt), "task-289", "main", role, task_context={"pr_number": PR_NUMBER})
    assert reviewed == sha_a
    assert (wt / "a.txt").read_text() == "the PR's code\n"


def test_a_valid_pin_needs_no_lookup_at_all(pr_repo, monkeypatch):
    """#287's contract, preserved: a pinned task is authoritative and must not
    be failed closed by a network problem it does not depend on."""
    clone, wt, sha_a, _ = pr_repo
    monkeypatch.setattr(
        sv, "_resolve_pr_head_branch",
        lambda *a, **k: pytest.fail("asked GitHub while pinned"))
    assert sv._prepare_worktree_for_task(
        str(wt), "task-289", "main", "reviewer",
        task_context={"pr_number": PR_NUMBER, "reviewed_sha": sha_a}) == sha_a


def test_a_task_with_no_pr_number_still_uses_its_branch(pr_repo, monkeypatch):
    """⛔Scope. Fail-closed applies to tasks that CLAIM a PR. A review dispatched
    directly against a branch has no PR head to miss, and that workflow keeps
    working."""
    clone, wt, sha_a, _ = pr_repo
    monkeypatch.setattr(
        sv, "_resolve_pr_head_branch",
        lambda *a, **k: pytest.fail("looked up a PR for a task that named none"))
    assert sv._prepare_worktree_for_task(
        str(wt), "task-289", PR_BRANCH, "reviewer", task_context={}) == sha_a


def test_the_implementer_is_unaffected(pr_repo, monkeypatch):
    """The implementer starts from main by design (#140); it is not reviewing an
    artifact, so there is nothing to fail closed about."""
    clone, wt, sha_a, main_tip = pr_repo
    monkeypatch.setattr(sv, "_resolve_pr_head_branch", lambda *a, **k: None)
    sv._prepare_worktree_for_task(str(wt), "task-289", "agent/x", "implementer",
                                  task_context={"pr_number": PR_NUMBER})
    assert _sha(wt) == main_tip


def test_failing_closed_writes_no_shared_ref(pr_repo, monkeypatch):
    """⛔#283 interaction: the refusal path must not leave a branch behind
    either. Checked with for-each-ref, not by reading the code."""
    clone, wt, sha_a, _ = pr_repo
    monkeypatch.setattr(sv, "_resolve_pr_head_branch", lambda *a, **k: None)
    before = set(_git("for-each-ref", "--format=%(refname)", "refs/heads",
                      cwd=clone).stdout.split())
    with pytest.raises(sv.WorktreeTargetUnresolved):
        sv._prepare_worktree_for_task(str(wt), "task-289", "main", "tester",
                                      task_context={"pr_number": PR_NUMBER})
    after = set(_git("for-each-ref", "--format=%(refname)", "refs/heads",
                     cwd=clone).stdout.split())
    assert after == before


# ── 2. the dispatcher launches nothing and records nothing ────────────


def _dispatch(tmp_path, monkeypatch, pr_repo, *, head=None):
    clone, wt, sha_a, main_tip = pr_repo
    spawned = []

    async def _fake_exec(*cmd, **kwargs):
        spawned.append(list(cmd))

        class _P:
            returncode, pid = 0, 1

            async def wait(self):
                return 0
        return _P()

    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    state = tmp_path / "state.json"
    state.write_text(json.dumps({"port": 0, "worktrees": {"gemini": str(wt)}}))
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.delenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", raising=False)
    monkeypatch.setenv("AGENT_CREW_BASE", str(tmp_path / "lockbase"))
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(sv, "_resolve_pr_head_branch", lambda *a, **k: head)

    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, pane_map={}, port=0, state_path=str(state),
                     project="demo", watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        q = TaskQueue(db)
        q.enqueue(TaskRequest(task_id="test-289", task_type="test", description="run",
                              branch="main", context={"pr_number": PR_NUMBER}))
        task = q.dequeue(role="tester")
        assert task is not None
        asyncio.run(app.state.dispatch_task(task, "tester"))
        row = {t.task_id: t for t in q.list_tasks()}["test-289"]
        attr = q.get_attribution("test-289")

    events_path = os.path.join(os.path.dirname(db), "context_events.jsonl")
    events = ([json.loads(line) for line in open(events_path)]
              if os.path.exists(events_path) else [])
    return spawned, row, attr, events, wt


def test_no_provider_is_launched_when_the_pr_head_is_unknown(tmp_path, monkeypatch,
                                                             pr_repo):
    """★★Acceptance: fail closed. A launched provider would produce a confident
    review of main that nothing downstream could distinguish from a real one."""
    spawned, row, attr, events, wt = _dispatch(tmp_path, monkeypatch, pr_repo)
    assert spawned == [], "a provider ran against an unresolved target"
    assert row.status == "needs_human", row.status


def test_no_tester_economics_are_recorded_for_a_test_that_never_ran(
        tmp_path, monkeypatch, pr_repo):
    """★★#278's treatment fields describe a test run. Emitting them here would
    attribute targeted/full-suite cost to a run that did not happen — the exact
    corruption #289 is about, one table over."""
    spawned, row, attr, events, wt = _dispatch(tmp_path, monkeypatch, pr_repo)
    assert not [e for e in events if e.get("event_type") == "test_scope_resolved"]
    if attr is not None:
        assert attr["effective_test_scope"] is None, attr["effective_test_scope"]


def test_the_worktree_is_left_alone_when_the_target_is_unknown(tmp_path, monkeypatch,
                                                              pr_repo):
    clone, wt0, sha_a, main_tip = pr_repo
    before = _sha(wt0)
    spawned, row, attr, events, wt = _dispatch(tmp_path, monkeypatch, pr_repo)
    assert _sha(wt) == before != main_tip


def test_a_resolvable_head_still_dispatches(tmp_path, monkeypatch, pr_repo):
    """⛔The control, through the real dispatcher."""
    clone, wt0, sha_a, _ = pr_repo
    spawned, row, attr, events, wt = _dispatch(tmp_path, monkeypatch, pr_repo,
                                               head=PR_BRANCH)
    assert len(spawned) == 1
    assert _sha(wt) == sha_a
    assert row.status != "needs_human"
