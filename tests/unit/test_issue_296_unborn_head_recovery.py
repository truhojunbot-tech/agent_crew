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
                                                              repo, *, unused_tcp_port):
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
    app = create_app(db_path=db, pane_map={}, port=unused_tcp_port, state_path=str(state),
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


def test_a_clean_creation_still_succeeds(repo):
    """⛔The control. Atomicity must not cost the heal its normal path."""
    clone, wt = repo
    ref = f"refs/heads/{_break(wt)}"
    assert sv._heal_unborn_head(str(wt), "main", ref, what="test") is True
    assert _git("rev-parse", "HEAD", cwd=wt).returncode == 0


def test_creation_is_a_compare_and_swap(repo):
    """★★The argv, because the guarantee lives in it. `update-ref` must carry an
    expected-old value; without one the write is unconditional however careful
    the code around it looks."""
    clone, wt = repo
    _break(wt)
    seen = []
    real_run = sv.subprocess.run

    def spy(cmd, **kw):
        if isinstance(cmd, list) and "update-ref" in cmd:
            seen.append((cmd, kw))
        return real_run(cmd, **kw)

    import unittest.mock as mock
    with mock.patch.object(sv.subprocess, "run", spy):
        assert sv._heal_unborn_head(str(wt), "main", what="test") is True

    assert len(seen) == 1, seen
    cmd, kwargs = seen[0]
    args = cmd[cmd.index("update-ref"):]
    # Either spelling of compare-and-swap is fine; an unconditional
    # `update-ref <ref> <sha>` is not. `--stdin` + `create` says "verify it does
    # not exist" outright and needs no hash-algorithm-specific zero sentinel,
    # so it is accepted alongside the expected-old argv form.
    stdin_create = "--stdin" in args and "create " in (kwargs.get("input") or "")
    expected_old = len(args) == 4 and set(args[3]) in ({"0"}, set())
    assert stdin_create or expected_old, f"unconditional write: {args} {kwargs.get('input')!r}"


def test_a_branch_created_between_check_and_write_is_not_overwritten(repo):
    """★★The race itself. Another worktree creates the branch after the heal has
    looked and before it writes; the write must lose rather than clobber."""
    clone, wt = repo
    branch = _break(wt)
    intruder = _git("rev-parse", "origin/main", cwd=wt).stdout.strip()
    real_run = sv.subprocess.run
    raced = {"done": False}

    def spy(cmd, **kw):
        # The heal's existence probe is the moment the window opens: answer
        # "absent" (as it truly is), then let the other worktree win.
        if (isinstance(cmd, list) and "rev-parse" in cmd and "--verify" in cmd
                and branch in " ".join(cmd) and not raced["done"]):
            result = real_run(cmd, **kw)
            raced["done"] = True
            real_run(["git", "-C", str(clone), "branch", branch, intruder],
                     capture_output=True, text=True)
            return result
        return real_run(cmd, **kw)

    import unittest.mock as mock
    with mock.patch.object(sv.subprocess, "run", spy):
        healed = sv._heal_unborn_head(str(wt), "main", what="test")

    assert raced["done"], "the race never happened — this test proves nothing"
    assert healed is False, "the heal reported success after losing the race"
    assert _git("rev-parse", branch, cwd=clone).stdout.strip() == intruder, \
        "the other worktree's ref was overwritten"


def test_losing_the_race_is_reported_loudly(repo, caplog):
    """⛔A heal that quietly gives up leaves an unusable worktree and an
    operator with no idea why. The refusal downstream is only actionable if the
    reason reached the log."""
    import logging

    clone, wt = repo
    branch = _break(wt)
    _git("branch", branch, _git("rev-parse", "origin/main", cwd=wt).stdout.strip(),
         cwd=clone)
    with caplog.at_level(logging.ERROR, logger="agent_crew.server"):
        assert sv._heal_unborn_head(str(wt), "main", what="test") is False
    assert branch in caplog.text


# ── 5. the refusal names the right cause ──────────────────────────────


def test_an_unhealthy_worktree_is_not_reported_as_a_pr_problem(tmp_path,
                                                               monkeypatch, repo, *, unused_tcp_port):
    """★★Review of PR #298, P2. Both handlers hardcoded `pr_head_unresolved`, so
    an operator reading the failure of a worktree whose HEAD does not resolve
    was sent to look at a PR that is perfectly fine."""
    import asyncio

    from fastapi.testclient import TestClient

    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue
    from agent_crew.server import create_app

    wt = _unhealable(repo)
    spawned = []

    async def _fake_exec(*cmd, **kwargs):
        spawned.append(list(cmd))

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
    app = create_app(db_path=db, pane_map={}, port=unused_tcp_port, state_path=str(state),
                     project="demo", watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        q = TaskQueue(db)
        q.enqueue(TaskRequest(task_id="t-298", task_type="implement",
                              description="go", branch="main", context={}))
        task = q.dequeue(role="implementer")
        asyncio.run(app.state.dispatch_task(task, "implementer"))
        row = {t.task_id: t for t in q.list_tasks()}["t-298"]

    assert spawned == []
    assert row.status == "needs_human"
    assert "worktree_unhealthy" in (row.summary or ""), row.summary
    assert "pr_head_unresolved" not in (row.summary or ""), row.summary


def test_each_refusal_carries_its_own_reason():
    """⛔The reason travels with the exception category rather than being
    written at the call site, so a third refusal cannot inherit the wrong one
    by being caught in the same handler."""
    assert sv.WorktreeUnhealthy.reason == "worktree_unhealthy"
    assert sv.WorktreeTargetUnresolved.reason == "pr_head_unresolved"
    assert sv.WorktreePrepRefused.reason  # a usable default for any future one
