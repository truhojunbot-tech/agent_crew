"""#280 — agent_crew must not move a branch ref it does not own.

Reported from quota-ops: a feature branch's local ref in the shared clone
`~/alfred/repos/quota-ops` was silently reset to `main`'s tip three times in one
session, each time while a `crew discuss --branch fix/compact-detection-history-limit-45`
round ran against that same branch name.

The premise checks out. The dispatcher's worktrees are `git worktree add`-based
off the shared clone, so `refs/heads/*` is one namespace:

    $ git -C ~/alfred/repos/quota-ops worktree list
    ~/alfred/repos/quota-ops                  ee6a9a2 [main]
    ~/.agent_crew/worktrees/quota-ops/claude  3dcf84b [fix/compact-detection-history-limit-45]

⚠️The reporter's theory named the *reviewer* as the culprit. It is not: the
  reviewer and tester check out derived names (`review/<id>`, `test/<id>`) and
  never touch the branch under review. The implementer path does —
  `git checkout -B <task.branch> origin/main` — and a `discuss` task dispatched
  to claude runs as `implementer`, so `--branch fix/…` force-moved that exact
  ref to `main`'s tip. Same conclusion, different line of code, and the
  distinction matters because fixing the reviewer would have fixed nothing.

The rule this pins: agent_crew force-moves refs only inside its OWN namespaces
(`agent/`, `review/`, `test/`). Every other branch name is somebody's work and
gets a detached checkout, which touches no ref at all.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from agent_crew.server import _agent_crew_owns_branch, _prepare_worktree_for_task

CALLER_BRANCH = "fix/compact-detection-history-limit-45"


def _git_calls(cmds):
    return [c for c in cmds if isinstance(c, list) and c[:2] == ["git", "-C"]]


def _run_prepare(task_branch, role, *, rc=0):
    cmds = []

    def fake_run(cmd, **_kw):
        cmds.append(cmd)
        return MagicMock(returncode=rc, stderr="", stdout="")

    with patch("agent_crew.server.subprocess.run", side_effect=fake_run):
        _prepare_worktree_for_task("/wt/claude", "task-abc123", task_branch, role)
    return _git_calls(cmds)


def _forced(git_calls):
    """Branch names this run force-moved, i.e. every `checkout -B <name>`."""
    out = []
    for c in git_calls:
        if "checkout" in c and "-B" in c:
            out.append(c[c.index("-B") + 1])
    return out


# ── 1. who owns which namespace ───────────────────────────────────────


@pytest.mark.parametrize("branch", [
    "agent/task-abc123", "agent/quota-ops/claude", "agent/claude/280-fix",
    "review/ab12cd34", "test/ff00ff00",
])
def test_agent_crew_owns_its_own_namespaces(branch):
    assert _agent_crew_owns_branch(branch) is True


@pytest.mark.parametrize("branch", [
    CALLER_BRANCH, "main", "dev", "master", "feature/x", "fix/foo",
    "agentic/x",          # ⛔prefix lookalike — not the `agent/` namespace
    "agents/x", "myagent/x", "", "   ",
])
def test_everything_else_belongs_to_somebody_else(branch):
    assert _agent_crew_owns_branch(branch) is False


# ── 2. the incident, at the argv level ────────────────────────────────


def test_a_callers_branch_is_never_force_moved(  ):
    """★★The reported bug. `checkout -B <caller's branch>` is what moved the ref."""
    git = _run_prepare(CALLER_BRANCH, "implementer")
    assert CALLER_BRANCH not in _forced(git), \
        f"force-moved a branch agent_crew does not own: {_forced(git)}"


def test_a_callers_branch_is_checked_out_detached_instead():
    git = _run_prepare(CALLER_BRANCH, "implementer")
    detach = [c for c in git if "checkout" in c and "--detach" in c]
    assert detach, f"no detached checkout; calls were {[' '.join(c[2:]) for c in git]}"
    assert f"origin/{CALLER_BRANCH}" in " ".join(detach[0]), \
        "detached somewhere other than that branch's own remote tip"


def test_main_is_never_force_moved(  ):
    """★★`crew discuss` defaults to `--branch main`, so this was the common path
    even when nobody passed a branch — and `main` is the ref most likely to be
    checked out in the shared clone doing unrelated work."""
    assert "main" not in _forced(_run_prepare("main", "implementer"))


def test_the_detach_falls_back_to_main_when_the_branch_has_no_remote(  ):
    """A brand-new branch name has no `origin/<name>` yet. Falling back keeps
    the task runnable without inventing a ref."""
    git = _run_prepare("brand-new-thing", "implementer", rc=1)   # every git call fails
    detach = [c for c in git if "checkout" in c and "--detach" in c]
    assert len(detach) == 2, "expected a retry at origin/main after the first detach failed"
    assert "origin/main" in " ".join(detach[-1])


# ── 3. what must NOT change ───────────────────────────────────────────


def test_the_derived_implementer_branch_still_gets_a_real_branch(  ):
    """⛔The control. #140's fresh-branch-per-task is agent_crew's own namespace
    and its whole point is a branch the agent can commit and push. Detaching
    everything would 'fix' the bug by breaking the implementer."""
    git = _run_prepare("", "implementer")
    assert any(b.startswith("agent/") for b in _forced(git)), \
        "the implementer lost its own working branch"


def test_an_explicitly_named_agent_branch_is_still_ours(  ):
    assert "agent/feat-xyz" in _forced(_run_prepare("agent/feat-xyz", "implementer"))


@pytest.mark.parametrize("role, prefix", [("reviewer", "review/"), ("tester", "test/")])
def test_reviewer_and_tester_are_unchanged(role, prefix):
    """They were never the culprit — they already use derived names. Pinned so a
    later 'consistency' cleanup does not detach them and take away the branch
    their own tooling expects."""
    forced = _forced(_run_prepare(CALLER_BRANCH, role))
    assert any(b.startswith(prefix) for b in forced)
    assert CALLER_BRANCH not in forced


# ── 4. against a real repository ──────────────────────────────────────


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          timeout=60)


@pytest.fixture
def shared_clone(tmp_path):
    """origin + a clone + a `git worktree` off it — the real topology."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   capture_output=True, check=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(origin), str(clone)], capture_output=True,
                   check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        _git("config", k, v, cwd=clone)
    (clone / "a.txt").write_text("one\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-m", "first", cwd=clone)
    _git("push", "-u", "origin", "main", cwd=clone)

    # the caller's feature branch, pushed, then left checked out in the clone
    _git("checkout", "-b", CALLER_BRANCH, cwd=clone)
    (clone / "a.txt").write_text("two\n")
    _git("commit", "-am", "the caller's work", cwd=clone)
    _git("push", "-u", "origin", CALLER_BRANCH, cwd=clone)
    _git("checkout", "main", cwd=clone)

    # main moves on, so a reset-to-main is unmistakable
    (clone / "b.txt").write_text("main moved\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-m", "main moves on", cwd=clone)
    _git("push", "origin", "main", cwd=clone)

    wt = tmp_path / "wt"
    _git("worktree", "add", "--detach", str(wt), "HEAD", cwd=clone)
    return clone, wt


def _sha(repo, ref):
    return _git("rev-parse", ref, cwd=repo).stdout.strip()


def test_the_callers_ref_does_not_move(shared_clone):
    """★★The incident reproduced end to end, with real git.

    Before the fix this asserts the exact reported symptom: the ref moves to
    `main`'s tip, in a repository the dispatcher was never asked to touch."""
    clone, wt = shared_clone
    before = _sha(clone, CALLER_BRANCH)
    main_tip = _sha(clone, "main")
    assert before != main_tip, "fixture is degenerate — the branch already is main"

    _prepare_worktree_for_task(str(wt), "task-abc123", CALLER_BRANCH, "implementer")

    after = _sha(clone, CALLER_BRANCH)
    where = "to main's tip — the exact reported symptom" if after == main_tip else "elsewhere"
    assert after == before, f"the caller's branch ref moved {where}"


def test_the_worktree_still_lands_on_that_branchs_content(shared_clone):
    """⛔Not moving the ref is only half of it. The task still has to see the
    code it was dispatched for, or the fix trades a data hazard for a
    correctness one."""
    clone, wt = shared_clone
    _prepare_worktree_for_task(str(wt), "task-abc123", CALLER_BRANCH, "implementer")
    assert _sha(wt, "HEAD") == _sha(clone, f"origin/{CALLER_BRANCH}")
    assert (wt / "a.txt").read_text() == "two\n"


def test_the_worktree_head_is_detached_not_owning_the_branch(shared_clone):
    clone, wt = shared_clone
    _prepare_worktree_for_task(str(wt), "task-abc123", CALLER_BRANCH, "implementer")
    assert _git("symbolic-ref", "-q", "HEAD", cwd=wt).returncode != 0, \
        "HEAD is on a branch, so a later reset in this worktree would move a shared ref"


# ── 5. the other shared-ref mover, and the telemetry it must not break ──


def test_the_cli_worktree_sync_does_not_force_move_main():
    """★★Same hazard, second site. `crew`'s worktree-sync did
    `checkout -B <main_branch> origin/<main_branch>`, which force-moves `main`
    for the caller's clone too — discarding any local-only commits there. It is
    also exactly the shape the reporter guessed at: a cleanup that means "leave
    this worktree on main" but implements it by moving the ref."""
    import inspect

    from agent_crew import cli

    source = inspect.getsource(cli)
    assert '"checkout", "-B", main_branch' not in source, \
        "worktree sync still force-moves the shared main ref"
    assert '"checkout", "--detach", f"origin/{main_branch}"' in source


def test_a_detached_worktree_still_reports_which_branch_the_work_is_about(
        shared_clone, tmp_path, monkeypatch):
    """⛔A fix that erases telemetry is not free. On a detached worktree
    `rev-parse --abbrev-ref HEAD` returns the literal string "HEAD", which joins
    to nothing in quota's economics — so attribution falls back to the branch
    the task was dispatched for.

    Runs against the REAL worktree with prep enabled: a stubbed one reports `''`
    rather than `"HEAD"`, so it would pin the wrong condition entirely."""
    import asyncio
    import json

    from fastapi.testclient import TestClient

    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue
    from agent_crew.server import create_app

    clone, wt = shared_clone

    async def _fake_exec(*cmd, **kwargs):
        class _P:
            returncode, pid = 0, 1

            async def wait(self):
                return 0
        return _P()

    state = tmp_path / "state.json"
    state.write_text(json.dumps({"port": 0, "worktrees": {"claude": str(wt)}}))
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.delenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", raising=False)
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec", _fake_exec)

    db = str(tmp_path / "tasks.db")
    app = create_app(db_path=db, pane_map={}, port=0, state_path=str(state),
                     project="demo", watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        q = TaskQueue(db)
        q.enqueue(TaskRequest(task_id="impl-280", task_type="implement",
                              description="go", branch=CALLER_BRANCH, context={}))
        task = q.dequeue(role="implementer")
        asyncio.run(app.state.dispatch_task(task, "implementer"))
        row = q.get_attribution("impl-280")

    # the precondition this test exists for
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt).stdout.strip() == "HEAD"
    assert row["git_branch"] == CALLER_BRANCH, \
        f"attribution lost the branch identity (got {row['git_branch']!r})"
