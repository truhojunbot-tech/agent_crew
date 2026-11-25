"""#1065: a defaulted `--branch` means the resolved repo default, and a base
strictly behind that default is refused unless `--allow-stale-base` is given.

Live evidence: on a repo whose default is `master`, a stale `origin/main`
73 commits behind made `crew run` sync worktrees to the literal `main`; the
implementer's PR then reverted two safety commits (trader PR #292).
"""

import asyncio
import subprocess

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from agent_crew import server
from agent_crew.cli import _sync_worktrees_to_main, crew
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import WorktreeBaseStale, _prepare_worktree_for_task
from agent_crew.worktree_default_branch import stale_base_gap


def _git(*args):
    return subprocess.run(
        ["git", *map(str, args)], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _repo(tmp_path, *, default="master", stale=None, ahead=None, advance=2):
    """A bare origin whose default is ``default``.

    ``stale`` names a branch pinned at the first commit while ``default`` gains
    ``advance`` more commits — the trader shape. ``ahead`` names a branch with
    one commit on top of the default's tip. Returns the worker clone (whose
    origin/HEAD points at ``default``) and the branch tips.
    """
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    worker = tmp_path / "worker"
    _git("init", "--bare", f"--initial-branch={default}", remote)
    _git("init", f"--initial-branch={default}", seed)
    _git("-C", seed, "config", "user.name", "Test User")
    _git("-C", seed, "config", "user.email", "test@example.invalid")
    (seed / "marker").write_text("base")
    _git("-C", seed, "add", "marker")
    _git("-C", seed, "commit", "-m", "base")
    _git("-C", seed, "remote", "add", "origin", remote)
    _git("-C", seed, "push", "origin", default)
    tips = {}
    if stale:
        _git("-C", seed, "push", "origin", f"HEAD:refs/heads/{stale}")
        tips[stale] = _git("-C", seed, "rev-parse", "HEAD")
    for i in range(advance):
        (seed / "marker").write_text(f"{default} {i}")
        _git("-C", seed, "commit", "-am", f"{default} advance {i}")
    _git("-C", seed, "push", "origin", default)
    tips[default] = _git("-C", seed, "rev-parse", "HEAD")
    if ahead:
        _git("-C", seed, "checkout", "-b", ahead)
        (seed / "marker").write_text(ahead)
        _git("-C", seed, "commit", "-am", ahead)
        _git("-C", seed, "push", "origin", ahead)
        tips[ahead] = _git("-C", seed, "rev-parse", "HEAD")
    _git("clone", remote, worker)
    return worker, tips


# --- the shared primitive -------------------------------------------------

def test_stale_base_gap_counts_only_a_strictly_behind_base(tmp_path):
    worker, _ = _repo(tmp_path, stale="main", ahead="feat/x", advance=3)

    assert stale_base_gap(str(worker), "main", "master") == 3
    assert stale_base_gap(str(worker), "master", "master") == 0
    assert stale_base_gap(str(worker), "feat/x", "master") == 0   # ahead
    assert stale_base_gap(str(worker), "not-pushed", "master") == 0
    assert stale_base_gap(str(worker), "main", "") == 0


# --- crew run ---------------------------------------------------------------

def _run(tmp_path, monkeypatch, worker, *args):
    import agent_crew.cli as cli
    import agent_crew.loop as loop

    monkeypatch.setattr(cli, "_read_state", lambda *_args: {
        "port": 0, "pane_ids": [], "worktrees": {"codex": str(worker)},
    })

    class Queue:
        def list_tasks(self):
            return []

        def get_result(self, task_id):
            return TaskResult(task_id, "failed", "fixture stops after enqueue")

    monkeypatch.setattr(cli, "_writable_queue", lambda *_args, **_kwargs: Queue())
    captured = []
    monkeypatch.setattr(loop, "enqueue_implement", lambda *a, **kw:
                        captured.append((a, kw)) or "impl-1065-cli")
    result = CliRunner().invoke(crew, [
        "run", "implement requested work", "--project", "test",
        "--db", str(tmp_path / "tasks.db"), "--no-tester", "--max-iter", "1",
        *args,
    ])
    return result, captured


@pytest.mark.usefixtures("admit_crew_run")
def test_a_defaulted_branch_syncs_and_preps_from_resolved_master(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)
    worker, tips = _repo(tmp_path, stale="main")

    result, captured = _run(tmp_path, monkeypatch, worker)

    assert result.exit_code == 0, result.output
    assert "Syncing worktrees to origin/master" in result.output
    (_queue, _task, branch), kwargs = captured[0]
    assert branch == "master"
    context = kwargs["context"]
    assert context["base_branch"] == "master"
    assert context["crew_run_branch"] is False
    assert "allow_stale_base" not in context
    assert context["sync_landed_bases"]["codex"] == {
        "requested_ref": "origin/master", "actual_ref": "origin/master",
        "sha": tips["master"], "status": "known",
    }
    assert _git("-C", worker, "rev-parse", "HEAD") == tips["master"]
    assert _prepare_worktree_for_task(
        str(worker), "impl-1065-cli", branch, "implementer", context,
    ) == tips["master"]


@pytest.mark.usefixtures("admit_crew_run")
def test_b_explicit_branch_behind_default_is_refused_with_names_and_gap(
        tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)
    worker, tips = _repo(tmp_path, stale="main", advance=3)

    result, captured = _run(tmp_path, monkeypatch, worker, "--branch", "main")

    assert result.exit_code != 0
    assert captured == []
    assert "'main'" in result.output and "'master'" in result.output
    assert "3 commits behind" in result.output
    assert "--allow-stale-base" in result.output


@pytest.mark.usefixtures("admit_crew_run")
def test_b_configured_main_branch_behind_default_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_MAIN_BRANCH", "main")
    worker, _ = _repo(tmp_path, stale="main")

    result, captured = _run(tmp_path, monkeypatch, worker)

    assert result.exit_code != 0
    assert captured == []
    assert "'main'" in result.output and "'master'" in result.output
    assert "2 commits behind" in result.output


@pytest.mark.usefixtures("admit_crew_run")
def test_c_allow_stale_base_proceeds_as_before(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)
    worker, tips = _repo(tmp_path, stale="main")

    result, captured = _run(tmp_path, monkeypatch, worker,
                            "--branch", "main", "--allow-stale-base")

    assert result.exit_code == 0, result.output
    assert "Warning" in result.output and "behind" in result.output
    (_queue, _task, branch), kwargs = captured[0]
    assert branch == "main"
    context = kwargs["context"]
    assert context["base_branch"] == "master"
    assert context["crew_run_branch"] is True
    assert context["allow_stale_base"] is True
    assert context["sync_landed_bases"]["codex"]["requested_ref"] == "origin/main"
    assert context["sync_landed_bases"]["codex"]["sha"] == tips["main"]


@pytest.mark.usefixtures("admit_crew_run")
def test_d_repo_whose_default_is_main_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)
    worker, tips = _repo(tmp_path, default="main", ahead="feat/live")

    result, captured = _run(tmp_path, monkeypatch, worker)
    assert result.exit_code == 0, result.output
    (_queue, _task, branch), kwargs = captured[0]
    assert branch == "main"
    assert kwargs["context"]["base_branch"] == "main"
    assert kwargs["context"]["crew_run_branch"] is False
    assert kwargs["context"]["sync_landed_bases"]["codex"] == {
        "requested_ref": "origin/main", "actual_ref": "origin/main",
        "sha": tips["main"], "status": "known",
    }

    # An explicit run branch ahead of the default is not stale.
    result, captured = _run(tmp_path, monkeypatch, worker, "--branch", "feat/live")
    assert result.exit_code == 0, result.output
    (_queue, _task, branch), kwargs = captured[0]
    assert branch == "feat/live"
    assert kwargs["context"]["crew_run_branch"] is True

    # A run branch origin does not have yet is not stale either.
    result, captured = _run(tmp_path, monkeypatch, worker, "--branch", "feat/new")
    assert result.exit_code == 0, result.output
    assert captured[0][0][2] == "feat/new"


# --- server prep --------------------------------------------------------------

def test_e_prep_refuses_context_base_behind_default(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)
    worker, tips = _repo(tmp_path, stale="main", advance=3)
    _git("-C", worker, "checkout", "--detach", "origin/main")

    with pytest.raises(WorktreeBaseStale, match="'main'.*3 commits.*'master'") as exc:
        _prepare_worktree_for_task(
            str(worker), "impl-1065", "fix/output", "implementer",
            {"base_branch": "main"}, "implement",
        )
    assert exc.value.reason == "stale_base_branch"
    assert _git("-C", worker, "rev-parse", "HEAD") == tips["main"]

    # The override keeps today's behaviour; a non-stale context base passes.
    assert _prepare_worktree_for_task(
        str(worker), "impl-1065", "fix/output", "implementer",
        {"base_branch": "main", "allow_stale_base": True}, "implement",
    ) == tips["main"]
    assert _prepare_worktree_for_task(
        str(worker), "impl-1065", "fix/output", "implementer",
        {"base_branch": "master"}, "implement",
    ) == tips["master"]


def _fail_default_fetch(monkeypatch, worker, default, fetched):
    """Fetch of ``default`` times out; every other fetch runs for real."""
    real = server.git_fetch_origin

    def fetch(repo_cwd, *branches):
        fetched.append(tuple(branches))
        if default in branches:
            return subprocess.CompletedProcess(
                ["git", "fetch", "origin", default], 124, "", "fetch timed out")
        return real(repo_cwd, *branches)
    monkeypatch.setattr(server, "git_fetch_origin", fetch)


def test_e_prep_refuses_when_the_default_cannot_be_refreshed(tmp_path, monkeypatch):
    """A failed default fetch must refuse, not fall back to a cached ref (PR #1066 review).

    The clone's tracking ref for the default is pinned at the stale base, so a
    gap measured from cached refs is 0 while remote master is 3 commits ahead.
    """
    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)
    worker, tips = _repo(tmp_path, stale="main", advance=3)
    _git("-C", worker, "update-ref", "refs/remotes/origin/master", tips["main"])
    _git("-C", worker, "checkout", "--detach", "origin/main")
    fetched = []
    _fail_default_fetch(monkeypatch, worker, "master", fetched)

    with pytest.raises(server.WorktreeFetchFailed, match="origin/master.*timed out") as exc:
        _prepare_worktree_for_task(
            str(worker), "impl-1065", "fix/output", "implementer",
            {"base_branch": "main"}, "implement",
        )
    assert exc.value.reason == "prep_fetch_failed"
    assert ("master",) in fetched
    assert _git("-C", worker, "rev-parse", "HEAD") == tips["main"]
    assert _git("-C", worker, "rev-parse", "origin/master") == tips["main"]


def test_e_dispatcher_refuses_when_default_fetch_fails_without_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)
    worker, tips = _repo(tmp_path, stale="main", advance=3)
    _git("-C", worker, "update-ref", "refs/remotes/origin/master", tips["main"])
    _git("-C", worker, "checkout", "--detach", "origin/main")
    _fail_default_fetch(monkeypatch, worker, "master", [])

    db = str(tmp_path / "tasks.db")
    notices = []
    app = server.create_app(
        db, worktree_map={"implementer": str(worker)},
        identity_required=False, watchdog_disabled=True, anomaly_disabled=True,
        coordinator_notify_fn=lambda message: notices.append(message) or True,
    )
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest("impl-1065", "implement", "work", branch="fix/work",
                              project="demo",
                              context={"risk_tier": 1, "base_branch": "main"}))
    monkeypatch.setattr(server, "_ensure_role_protocol", lambda *a, **kw: True)
    launches = []

    async def no_launch(*args, **kwargs):
        launches.append(args)
        raise AssertionError("worker launched although the default could not be refreshed")
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", no_launch)

    with TestClient(app):
        task = queue.dequeue(role="implementer")
        assert task is not None
        asyncio.run(app.state.dispatch_task(task, "implementer"))

    assert launches == []
    assert queue.get_result("impl-1065").error_info["reason"] == "prep_fetch_failed"
    assert _git("-C", worker, "rev-parse", "HEAD") == tips["main"]
    assert len(notices) == 1
    assert "prep_fetch_failed" in notices[0]


def test_e_dispatcher_refuses_stale_base_with_one_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)
    worker, tips = _repo(tmp_path, stale="main")
    _git("-C", worker, "checkout", "--detach", "origin/main")
    _git("-C", worker, "switch", "-c", "unrelated")
    (worker / "other.txt").write_text("other task\n")
    _git("-C", worker, "add", ".")
    _git("-C", worker, "config", "user.email", "crew@example.test")
    _git("-C", worker, "config", "user.name", "Crew Test")
    _git("-C", worker, "commit", "-m", "other task")
    unrelated_head = _git("-C", worker, "rev-parse", "HEAD")

    db = str(tmp_path / "tasks.db")
    notices = []
    app = server.create_app(
        db, worktree_map={"implementer": str(worker)},
        identity_required=False, watchdog_disabled=True, anomaly_disabled=True,
        coordinator_notify_fn=lambda message: notices.append(message) or True,
    )
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest("impl-1065", "implement", "work", branch="fix/work",
                              project="demo",
                              context={"risk_tier": 1, "base_branch": "main"}))
    monkeypatch.setattr(server, "_ensure_role_protocol", lambda *a, **kw: True)
    launches = []

    async def no_launch(*args, **kwargs):
        launches.append(args)
        raise AssertionError("worker launched from a stale base")
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", no_launch)

    with TestClient(app):
        task = queue.dequeue(role="implementer")
        assert task is not None
        asyncio.run(app.state.dispatch_task(task, "implementer"))

    assert launches == []
    assert queue.get_task_status("impl-1065") == "needs_human"
    assert queue.get_result("impl-1065").error_info["reason"] == "stale_base_branch"
    assert _git("-C", worker, "rev-parse", "HEAD") == unrelated_head
    assert len(notices) == 1
    assert "stale_base_branch" in notices[0]


def test_sync_with_resolved_base_lands_on_master_not_stale_main(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)
    worker, tips = _repo(tmp_path, stale="main")

    landed = _sync_worktrees_to_main({"codex": str(worker)}, base_branch="master")

    assert landed["codex"]["status"] == "known"
    assert landed["codex"]["sha"] == tips["master"]
    assert _git("-C", worker, "rev-parse", "HEAD") == tips["master"]


def test_allow_stale_base_is_admitted_at_post_tasks(tmp_db):
    """The server override is read from ``task_context`` (prep check), so a
    non-CLI producer must be able to POST it. Before this entry the #941
    allowlist refused it with 422 and the documented override was unreachable."""
    app = server.create_app(tmp_db, project="demo", pane_map={}, worktree_map={},
                            watchdog_disabled=True, anomaly_disabled=True)
    payload = {"task_id": "impl-1065-stale", "task_type": "implement",
               "description": "work", "branch": "agent/1065-stale",
               "context": {"risk_tier": 1, "base_branch": "main",
                           "allow_stale_base": True}}
    with TestClient(app) as client:
        resp = client.post("/tasks", json=payload,
                           headers={"X-Agent-Crew-Project": "demo"})
    assert resp.status_code == 201, resp.text
    stored = TaskQueue(tmp_db).get_task("impl-1065-stale")
    assert stored is not None
    assert stored.context["allow_stale_base"] is True


# --- crew run: the comparison refs must be fresh (PR #1066 review r2) ---------

def _cache_default_at_stale_tip(worker, tips, *, default="master", stale="main"):
    """Pin the clone's cached origin/<default> at the stale tip.

    This is the shape after an interrupted or failed refresh: the remote's
    default has moved on, but the local tracking ref still sits where the
    stale branch is, so a comparison against cached refs measures a gap of 0.
    """
    _git("-C", worker, "update-ref", f"refs/remotes/origin/{default}", tips[stale])
    assert stale_base_gap(str(worker), stale, default) == 0


@pytest.mark.usefixtures("admit_crew_run")
def test_b_failed_sync_fetch_refuses_explicit_stale_base(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)
    worker, tips = _repo(tmp_path, stale="main", advance=3)
    _cache_default_at_stale_tip(worker, tips)
    # Every fetch now fails (exit 128): origin points at a repository that is
    # not there. Only cached refs remain, and they say main == master.
    _git("-C", worker, "remote", "set-url", "origin", str(tmp_path / "gone.git"))

    result, captured = _run(tmp_path, monkeypatch, worker, "--branch", "main")

    assert result.exit_code != 0, result.output
    assert captured == []
    assert "origin/master" in result.output
    assert "--allow-stale-base" in result.output
    assert _git("-C", worker, "rev-parse", "HEAD") == tips["main"]

    # The explicit override keeps today's behaviour, with a warning.
    result, captured = _run(tmp_path, monkeypatch, worker,
                            "--branch", "main", "--allow-stale-base")
    assert result.exit_code == 0, result.output
    assert "Warning" in result.output and "origin/master" in result.output
    (_queue, _task, branch), kwargs = captured[0]
    assert branch == "main"
    assert kwargs["context"]["allow_stale_base"] is True


@pytest.mark.usefixtures("admit_crew_run")
def test_b_timed_out_sync_fetch_refuses_explicit_stale_base(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)
    worker, tips = _repo(tmp_path, stale="main", advance=3)
    _cache_default_at_stale_tip(worker, tips)

    real_run = subprocess.run
    fetches = []

    def timing_out_fetch(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and "fetch" in cmd:
            fetches.append(list(cmd))
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", timing_out_fetch)

    result, captured = _run(tmp_path, monkeypatch, worker, "--branch", "main")

    assert fetches, "the run never tried to refresh origin"
    assert result.exit_code != 0, result.output
    assert captured == []
    assert "origin/master" in result.output
    assert "--allow-stale-base" in result.output
