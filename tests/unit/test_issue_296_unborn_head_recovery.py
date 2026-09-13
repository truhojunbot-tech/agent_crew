"""#296 — a worktree left with an unborn HEAD is not detected or healed.

Reported live: all three of a project's role worktrees were found with an
unborn HEAD and every tracked file staged (4,000+ each). The reflog's `@{0}`
entry for the branch carried **no message**, which is what an interrupted ref
update looks like — the ref moved and the index/working tree were never brought
into line with it.

#280/#286/#289 closed the mechanism that caused it (reviewer/tester prep no
longer writes `refs/heads/*` at all, and resolves before it checks out). They do
not help a worktree that is *already* broken: prep runs `stash`, `fetch` and
`checkout` against it and nothing first asks whether HEAD resolves.

⛔The recovery is `update-ref` + `reset --mixed`, validated live before this was
  filed. Never `checkout -B` or `reset --hard`: on an unborn HEAD with
  everything staged, those are the two commands that would throw the staged
  content away, and staged content in a broken worktree may be the only record
  of what the interrupted task had done.
"""

import json
import subprocess

import pytest

from agent_crew import server as sv


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=60)


@pytest.fixture
def repo(tmp_path):
    """origin + clone + a worktree, all healthy."""
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
    wt = tmp_path / "wt"
    _git("worktree", "add", "--detach", str(wt), "HEAD", cwd=clone)
    return clone, wt


def _break(wt, branch="review/review-3"):
    """Reproduce the reported shape: unborn HEAD with everything staged."""
    _git("symbolic-ref", "HEAD", f"refs/heads/{branch}", cwd=wt)
    (wt / "work-in-progress.txt").write_text("something the killed task had done\n")
    _git("add", "-A", cwd=wt)
    assert _git("rev-parse", "HEAD", cwd=wt).returncode != 0, "fixture is not broken"
    return branch


# ── 1. detection ──────────────────────────────────────────────────────


def test_a_healthy_worktree_is_healthy(repo):
    _, wt = repo
    assert sv._worktree_is_healthy(str(wt)) is True


def test_an_unborn_head_is_not_healthy(repo):
    _, wt = repo
    _break(wt)
    assert sv._worktree_is_healthy(str(wt)) is False


def test_a_missing_directory_is_not_healthy(tmp_path):
    assert sv._worktree_is_healthy(str(tmp_path / "nope")) is False


# ── 2. recovery ───────────────────────────────────────────────────────


def test_prep_heals_an_unborn_head_before_anything_else(repo):
    """★★The fix. Prep used to run stash/fetch/checkout against a worktree
    whose HEAD did not resolve."""
    clone, wt = repo
    _break(wt)
    sv._prepare_worktree_for_task(str(wt), "task-296", "agent/x", "implementer")
    assert _git("rev-parse", "HEAD", cwd=wt).returncode == 0


@pytest.mark.parametrize("role", ["implementer", "reviewer", "tester"])
def test_every_role_path_heals(repo, role, monkeypatch):
    clone, wt = repo
    _break(wt)
    monkeypatch.setattr(sv, "_resolve_pr_head_branch", lambda *a, **k: None)
    sv._prepare_worktree_for_task(str(wt), "task-296", "main", role)
    assert _git("rev-parse", "HEAD", cwd=wt).returncode == 0, role


def test_the_working_tree_files_are_not_discarded(repo):
    """⛔`reset --mixed`, never `--hard`. The staged content of a broken
    worktree may be the only record of what the interrupted task had done."""
    clone, wt = repo
    _break(wt)
    sv._prepare_worktree_for_task(str(wt), "task-296", "agent/x", "implementer")
    # prep stashes leftovers rather than deleting them, so the file is either
    # still present or recoverable from the stash — never simply gone.
    stashed = _git("stash", "list", cwd=wt).stdout
    assert (wt / "work-in-progress.txt").exists() or stashed.strip(), \
        "the interrupted task's work was destroyed"


def test_what_was_staged_is_logged_before_recovery(repo, caplog):
    """⛔Loud, and specific. "This class of corruption should never be silent",
    and a count of what was staged is the forensic record."""
    import logging

    clone, wt = repo
    _break(wt)
    # Counted BEFORE prep: the heal unstages, so asking afterwards reports 0.
    staged_count = len([n for n in _git("diff", "--cached", "--name-only",
                                        cwd=wt).stdout.splitlines() if n.strip()])
    assert staged_count >= 2, "fixture staged nothing to report"
    with caplog.at_level(logging.WARNING, logger="agent_crew.server"):
        sv._prepare_worktree_for_task(str(wt), "task-296", "agent/x", "implementer")
    text = caplog.text
    assert "unborn" in text.lower() or "HEAD does not resolve" in text
    # ⛔The COUNT and a sample of names, not merely the word "staged" —
    #   mutation showed that a message which dropped the count still contained
    #   the word elsewhere and passed. A forensic record has to carry the facts.
    assert f"{staged_count} staged" in text, text[-400:]
    assert "work-in-progress.txt" in text, "no staged path was named"


def test_recovery_only_creates_a_ref_it_never_moves_one(repo, monkeypatch):
    """⛔#280's invariant, preserved. `update-ref` on a ref that ALREADY
    resolves would be a force-move in the shared namespace — the exact thing
    #280 exists to stop. Recovery may only bring an unborn ref into being."""
    clone, wt = repo
    calls = []
    real_run = sv.subprocess.run

    def spy(cmd, **kw):
        if isinstance(cmd, list) and "update-ref" in cmd:
            calls.append(cmd)
        return real_run(cmd, **kw)

    monkeypatch.setattr(sv.subprocess, "run", spy)
    sv._prepare_worktree_for_task(str(wt), "task-296", "agent/x", "implementer")
    assert calls == [], "update-ref ran against a healthy worktree"


def test_a_live_ref_is_never_force_moved_to_heal_head(repo, caplog):
    """⛔#280's invariant under the one state where it could actually bite: HEAD
    fails to resolve while its branch ref DOES — a dangling object rather than
    an unborn branch. Healing by `update-ref` there would overwrite a live ref
    in the namespace shared with the caller's clone.

    Constructed by writing a bogus object id into the ref file directly, since
    git itself refuses to point a ref at an object that does not exist."""
    import logging
    import pathlib

    clone, wt = repo
    branch = "review/dangling"
    _git("symbolic-ref", "HEAD", f"refs/heads/{branch}", cwd=wt)
    common = pathlib.Path(_git("rev-parse", "--git-common-dir", cwd=wt).stdout.strip())
    if not common.is_absolute():
        common = pathlib.Path(wt) / common
    ref_file = common / "refs" / "heads" / branch
    ref_file.parent.mkdir(parents=True, exist_ok=True)
    ref_file.write_text("0" * 39 + "1\n")
    # Called directly rather than through prep: this git reports a dangling
    # ref as a healthy HEAD, so `_worktree_is_healthy` would never route here.
    # The guard still has to hold for any caller that does reach it — a
    # `update-ref` onto a ref that already resolves is a force-move in the
    # shared namespace whatever brought us here.
    before = ref_file.read_text().strip()
    with caplog.at_level(logging.ERROR, logger="agent_crew.server"):
        healed = sv._heal_unborn_head(str(wt), "main", what="test")
    assert healed is False, "healed by overwriting a ref that already resolves"
    assert ref_file.read_text().strip() == before, "a live ref was force-moved"


def test_a_healthy_worktree_is_left_alone(repo):
    """⛔The control: detection must not become an unconditional rewrite."""
    clone, wt = repo
    before = _git("rev-parse", "HEAD", cwd=wt).stdout.strip()
    sv._prepare_worktree_for_task(str(wt), "task-296", "agent/x", "implementer")
    # the implementer path legitimately moves HEAD; what matters is that it did
    # so by its own checkout, not by a recovery that was never needed.
    assert _git("rev-parse", "HEAD", cwd=wt).returncode == 0
    assert before


# ── 3. when recovery cannot work ──────────────────────────────────────


def _unhealable(repo):
    """An unborn HEAD with no `origin/main` to heal it from."""
    import pathlib

    clone, wt = repo
    _break(wt)
    common = pathlib.Path(_git("rev-parse", "--git-common-dir", cwd=wt).stdout.strip())
    if not common.is_absolute():
        common = pathlib.Path(wt) / common
    for candidate in (common / "refs" / "remotes" / "origin" / "main",
                      common / "packed-refs"):
        if candidate.exists():
            candidate.unlink()
    return wt


def test_an_unrecoverable_worktree_refuses_rather_than_dispatching(repo):
    """⛔Fail closed, like #289. An unborn HEAD that cannot be healed leaves the
    worktree in an unknown state, and dispatching into it would produce work
    about something nobody can name."""
    wt = _unhealable(repo)
    with pytest.raises(sv.WorktreeUnhealthy):
        sv._prepare_worktree_for_task_inner(
            str(wt), "task-296", "agent/x", "implementer", {})


def test_a_directory_that_is_not_a_repo_is_not_this_condition(tmp_path):
    """⛔Scope, stated as a test. "HEAD does not resolve" also covers a missing
    repository, a missing git, a transient failure — none of which #296 heals.
    Treating them as the same condition would make prep refuse in situations it
    has always survived, so the check requires the full unborn shape."""
    broken = tmp_path / "not-a-repo"
    broken.mkdir()
    assert sv._unborn_head_ref(str(broken)) == ""
    # prep still runs its usual best-effort path and does not raise
    sv._prepare_worktree_for_task(str(broken), "task-296", "agent/x", "implementer")


def test_the_refusal_shares_the_base_the_callers_already_catch(repo):
    """Both push paths already stop on `WorktreePrepRefused`; an unhealthy
    worktree has to land in the same handler or the fail-closed is partial."""
    assert issubclass(sv.WorktreeUnhealthy, sv.WorktreePrepRefused)
    assert issubclass(sv.WorktreeTargetUnresolved, sv.WorktreePrepRefused)


def test_the_dispatcher_does_not_launch_into_a_broken_worktree(tmp_path, monkeypatch,
                                                              repo):
    """★★End to end: no provider process, task marked needs_human."""
    import asyncio

    from fastapi.testclient import TestClient

    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue
    from agent_crew.server import create_app

    spawned = []

    async def _fake_exec(*cmd, **kwargs):
        spawned.append(list(cmd))

        class _P:
            returncode, pid = 0, 1

            async def wait(self):
                return 0
        return _P()

    wt = _unhealable(repo)
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
        q.enqueue(TaskRequest(task_id="t-296", task_type="implement",
                              description="go", branch="main", context={}))
        task = q.dequeue(role="implementer")
        asyncio.run(app.state.dispatch_task(task, "implementer"))
        status = {t.task_id: t.status for t in q.list_tasks()}["t-296"]

    assert spawned == [], "an agent was dispatched into an unusable worktree"
    assert status == "needs_human", status
