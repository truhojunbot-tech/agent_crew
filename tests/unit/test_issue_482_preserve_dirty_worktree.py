"""#482 — the post-task / orphan reset must not destroy uncommitted provider work.

2026-09-28 05:16Z, :8105: codex finished `impl-tok31-context-pack-shadow`
(67 tests passing) and hit its usage limit right before `git commit`. The task
failed with `exit_1`, the dispatcher's post-task reset ran `git checkout .` +
`git clean -fd`, and the change was gone — no stash, no dangling commit.

These tests use a REAL git worktree: the bug is what git does to the files,
so a mocked `subprocess.run` cannot show it.
"""

import asyncio
import json
import os
import subprocess

import pytest

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import _stash_dirty_worktree


@pytest.fixture(autouse=True)
def _declared_base_branch(monkeypatch):
    """These #482 tests exercise WIP preservation without a remote."""
    monkeypatch.setenv("AGENT_CREW_MAIN_BRANCH", "main")


def _git(wt, *args) -> str:
    return subprocess.run(["git", "-C", str(wt), *args], check=True,
                          capture_output=True, text=True).stdout


def _repo(path):
    path.mkdir(exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    (path / "app.py").write_text("x = 1\n")
    (path / "AGENTS.md").write_text("project agents\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _dirty(wt):
    """What codex left behind: a tracked edit and a new, untracked test."""
    (wt / "app.py").write_text("x = 2  # shadow_enabled\n")
    (wt / "test_shadow.py").write_text("def test_it():\n    pass\n")


def _stashes(wt) -> list[str]:
    return [s for s in _git(wt, "stash", "list").splitlines() if s]


def _is_clean(wt) -> bool:
    return _git(wt, "status", "--porcelain", "--", ".", ":(exclude).claude/CLAUDE.md",
                ":(exclude)AGENTS.md", ":(exclude)GEMINI.md").strip() == ""


# ── the helper ────────────────────────────────────────────────────────


def test_dirty_worktree_is_stashed_with_the_task_id(tmp_path):
    wt = _repo(tmp_path / "wt")
    _dirty(wt)

    ref = _stash_dirty_worktree(str(wt), "impl-tok31")

    assert ref and ref == _git(wt, "rev-parse", "refs/stash").strip()
    assert _stashes(wt) == ["stash@{0}: On main: agent_crew wip impl-tok31"]
    assert _is_clean(wt)
    # Both the tracked edit and the untracked file are recoverable.
    _git(wt, "stash", "pop")
    assert (wt / "app.py").read_text() == "x = 2  # shadow_enabled\n"
    assert (wt / "test_shadow.py").exists()


def test_stash_ref_wins_over_ignored_path_warning(tmp_path, monkeypatch):
    wt = _repo(tmp_path / "wt_warning")
    _dirty(wt)
    ignored = wt / ".claude"
    ignored.mkdir()
    (ignored / "CLAUDE.md").write_text("protocol")
    real_run = subprocess.run

    def warning_after_stash(argv, **kwargs):
        result = real_run(argv, **kwargs)
        if argv[3:5] == ["stash", "push"]:
            result.returncode = 1
            result.stderr = "warning: ignored protocol path"
        return result

    monkeypatch.setattr("agent_crew.server.subprocess.run", warning_after_stash)
    ref = _stash_dirty_worktree(str(wt), "task-warning")
    assert ref and ref == _git(wt, "rev-parse", "refs/stash").strip()
    assert _is_clean(wt)


def test_clean_worktree_creates_no_stash(tmp_path):
    wt = _repo(tmp_path / "wt")
    assert _stash_dirty_worktree(str(wt), "t-clean") == ""
    assert _stashes(wt) == []


def test_protocol_files_alone_are_not_worth_a_stash(tmp_path):
    """agent_crew rewrites these every task; stashing them would put an entry
    on the stack after every single task."""
    wt = _repo(tmp_path / "wt")
    (wt / "AGENTS.md").write_text("project agents\n<!-- agent_crew block -->\n")
    (wt / ".claude").mkdir()
    (wt / ".claude" / "CLAUDE.md").write_text("protocol\n")

    assert _stash_dirty_worktree(str(wt), "t-proto") == ""
    assert _stashes(wt) == []


def test_a_non_repo_does_not_raise(tmp_path):
    assert _stash_dirty_worktree(str(tmp_path), "t-x") is None


def test_stash_failure_is_distinct_from_a_clean_tree(tmp_path):
    wt = _repo(tmp_path / "wt")
    _dirty(wt)
    (wt / ".git" / "refs" / "stash.lock").write_text("locked")

    assert _stash_dirty_worktree(str(wt), "t-fail") is None
    assert (wt / "app.py").read_text() == "x = 2  # shadow_enabled\n"
    assert (wt / "test_shadow.py").exists()
    assert _stashes(wt) == []


def test_pre_dispatch_refuses_failed_stash_in_real_worktree(tmp_path):
    from agent_crew.server import WorktreeUnhealthy, _prepare_worktree_for_task_inner

    wt = _repo(tmp_path / "wt")
    _dirty(wt)
    (wt / ".git" / "refs" / "stash.lock").write_text("locked")

    with pytest.raises(WorktreeUnhealthy, match="manual recovery required"):
        _prepare_worktree_for_task_inner(str(wt), "t-fail", "main", "implementer")

    assert (wt / "app.py").read_text() == "x = 2  # shadow_enabled\n"
    assert (wt / "test_shadow.py").exists()
    assert _stashes(wt) == []


# ── the real post-task reset path ─────────────────────────────────────


def _dispatch(tmp_path, monkeypatch, unused_tcp_port, *, outcome, stash_failure=False):
    """Drive `_dispatch_task` end to end; the fake worker dirties the worktree
    while it "runs", exactly like codex editing files before exiting."""
    from fastapi.testclient import TestClient

    from agent_crew import server as sv
    from agent_crew.server import create_app

    wt = _repo(tmp_path / "claude")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "role_agents": {"implementer": "claude", "reviewer": "codex", "tester": "gemini"},
        "worktrees": {"claude": str(wt)}}))
    db = str(tmp_path / "t.db")

    async def _fake_exec(*cmd, **kwargs):
        _dirty(wt)
        if stash_failure:
            (wt / ".git" / "refs" / "stash.lock").write_text("locked")
        if outcome == "completed":
            TaskQueue(db).submit_result("t-1", TaskResult(
                task_id="t-1", status="completed", summary="done"))

        class _P:
            returncode = 1 if outcome == "exit_1" else 0
            pid = 4242

            async def communicate(self):
                return (b"", b"")

            async def wait(self):
                return self.returncode

            def kill(self):
                pass

            def terminate(self):
                pass

        return _P()

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(sv, "_dispatch_timeout_for_role", lambda role, task_context=None: 5)
    monkeypatch.setattr(sv.os, "killpg", lambda *a, **k: None)

    app = create_app(db_path=db, pane_map={}, port=unused_tcp_port, state_path=str(state),
                     project="p", watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        q = TaskQueue(db)
        q.enqueue(TaskRequest(task_id="t-1", task_type="implement",
                              description="do it", branch="main"))
        task = q.dequeue(role="implementer")
        asyncio.run(app.state.dispatch_task(task, "implementer"))
        row = next(t for t in q.list_tasks() if t.task_id == "t-1")
    return wt, row


def test_failed_task_leaves_a_named_stash_and_a_clean_tree(tmp_path, monkeypatch, unused_tcp_port):
    wt, row = _dispatch(tmp_path, monkeypatch, unused_tcp_port, outcome="exit_1")

    assert row.status == "failed"
    assert _stashes(wt) == ["stash@{0}: On main: agent_crew wip t-1"]
    assert _is_clean(wt), "the reset must still run so the next task starts clean"
    assert row.error_info["reason"] == "exit_1"
    assert row.error_info["wip_stash"] == _git(wt, "rev-parse", "refs/stash").strip()
    assert row.error_info["wip_worktree"] == str(wt)


def test_failed_task_preserves_dirty_tree_when_stash_fails(tmp_path, monkeypatch, unused_tcp_port):
    wt, row = _dispatch(tmp_path, monkeypatch, unused_tcp_port,
                        outcome="exit_1", stash_failure=True)

    assert row.status == "failed"
    assert row.error_info["wip_stash_error"] == "manual_recovery_required"
    assert row.error_info["wip_worktree"] == str(wt)
    assert (wt / "app.py").read_text() == "x = 2  # shadow_enabled\n"
    assert (wt / "test_shadow.py").exists()
    assert _stashes(wt) == []


def test_successful_task_is_reset_as_before_without_a_stash(tmp_path, monkeypatch, unused_tcp_port):
    wt, row = _dispatch(tmp_path, monkeypatch, unused_tcp_port, outcome="completed")

    assert row.status == "completed"
    assert _stashes(wt) == []
    assert _is_clean(wt)
    assert not (wt / "test_shadow.py").exists()


# ── the startup orphan re-queue path ──────────────────────────────────


@pytest.mark.parametrize("dirty", [True, False])
def test_orphan_requeue_stashes_only_a_dirty_worktree(tmp_db, tmp_path, monkeypatch, dirty):
    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    wt = _repo(tmp_path / "wt")
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="orphan-1", task_type="implement", description="d",
                          branch="main", context={"role": "implementer"}))
    assert q.dequeue(role="implementer", claim_source="dispatcher").task_id == "orphan-1"
    if dirty:
        _dirty(wt)

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", "60")
    app = create_app(db_path=tmp_db, pane_map={}, worktree_map={"implementer": str(wt)},
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        body = client.get("/tasks/orphan-1").json()

    assert body["status"] == "pending"
    assert _is_clean(wt)
    if dirty:
        assert _stashes(wt) == ["stash@{0}: On main: agent_crew wip orphan-1"]
        assert body["error_info"]["wip_stash"] == _git(wt, "rev-parse", "refs/stash").strip()
    else:
        assert _stashes(wt) == []
        assert not (body.get("error_info") or {}).get("wip_stash")
    assert os.path.isdir(wt)


def test_orphan_stash_failure_requires_recovery_without_reset(tmp_db, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from agent_crew.server import create_app

    wt = _repo(tmp_path / "wt")
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="orphan-1", task_type="implement", description="d",
                          branch="main", context={"role": "implementer"}))
    assert q.dequeue(role="implementer", claim_source="dispatcher").task_id == "orphan-1"
    _dirty(wt)
    (wt / ".git" / "refs" / "stash.lock").write_text("locked")

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", "60")
    app = create_app(db_path=tmp_db, pane_map={}, worktree_map={"implementer": str(wt)},
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        body = client.get("/tasks/orphan-1").json()

    assert body["status"] == "failed"
    assert body["error_info"]["wip_stash_error"] == "manual_recovery_required"
    assert body["error_info"]["wip_worktree"] == str(wt)
    assert (wt / "app.py").read_text() == "x = 2  # shadow_enabled\n"
    assert (wt / "test_shadow.py").exists()
    assert _stashes(wt) == []
