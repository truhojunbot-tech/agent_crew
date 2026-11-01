"""#839: resume a held task with the work saved by #482."""

import asyncio
import json
import subprocess

from fastapi.testclient import TestClient

from agent_crew import server
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue, _CEA_SYSTEM_SUCCESSOR_PROVENANCE


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _worktree(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q", "-b", "main")
    _git(wt, "config", "user.email", "t@example.com")
    _git(wt, "config", "user.name", "t")
    (wt / "app.py").write_text("value = 1\n")
    _git(wt, "add", "app.py")
    _git(wt, "commit", "-qm", "base")
    return wt


def _stash(wt, task_id):
    (wt / "app.py").write_text("value = 2\n")
    (wt / "new_test.py").write_text("assert True\n")
    sha = server._stash_dirty_worktree(str(wt), task_id)
    assert sha
    assert not (wt / "new_test.py").exists()
    return sha


def _dispatch_app(tmp_path, monkeypatch, wt, pushes, notices=None):
    monkeypatch.setenv("AGENT_CREW_DELIVERY", "push")
    monkeypatch.setattr(server, "_resolve_tmux_pane_target", lambda target: target)
    monkeypatch.setattr(server, "_pane_alive_for_push", lambda pane: True)
    monkeypatch.setattr(server, "_ensure_role_protocol", lambda *args, **kwargs: True)

    def prepare(*args, **kwargs):
        _git(wt, "reset", "--hard", "HEAD")
        _git(wt, "clean", "-fd")
        return _git(wt, "rev-parse", "HEAD")

    monkeypatch.setattr(server, "_prepare_worktree_for_task", prepare)
    db = str(tmp_path / "tasks.db")
    app = server.create_app(
        db_path=db, pane_map={"implementer": "%839", "codex": "%839"},
        worktree_map={"implementer": str(wt)},
        push_fn=lambda *args: pushes.append(args),
        coordinator_notify_fn=(lambda message: notices.append(message) or True)
        if notices is not None else None,
        watchdog_disabled=True, anomaly_disabled=True, identity_required=False,
        project="demo")
    return app, TaskQueue(db)


def _task(task_id, context=None):
    return TaskRequest(task_id=task_id, task_type="implement", description="resume work",
                       branch="main", project="demo", context=context or {})


def test_hold_resume_restores_tracked_and_untracked_before_push(tmp_path, monkeypatch):
    wt = _worktree(tmp_path)
    sha = _stash(wt, "impl-held")
    pushes = []
    app, queue = _dispatch_app(tmp_path, monkeypatch, wt, pushes)
    queue.enqueue(_task("impl-held"))
    queue.annotate_error_info("impl-held", {"wip_stash": sha})

    with TestClient(app):
        app.state.try_push_next("implementer")

    assert len(pushes) == 1
    assert (wt / "app.py").read_text() == "value = 2\n"
    assert (wt / "new_test.py").read_text() == "assert True\n"
    assert queue.get_task("impl-held").error_info["wip_restored"] == sha
    assert _git(wt, "rev-parse", "refs/stash") == sha


def test_restore_conflict_fails_without_push_and_keeps_stash(tmp_path, monkeypatch):
    wt = _worktree(tmp_path)
    sha = _stash(wt, "impl-conflict")
    (wt / "app.py").write_text("value = 3\n")
    _git(wt, "add", "app.py")
    _git(wt, "commit", "-qm", "new base")
    pushes = []
    notices = []
    app, queue = _dispatch_app(tmp_path, monkeypatch, wt, pushes, notices)
    queue.enqueue(_task("impl-conflict"))
    queue.annotate_error_info("impl-conflict", {"wip_stash": sha})

    with TestClient(app):
        app.state.try_push_next("implementer")

    row = queue.get_task("impl-conflict")
    assert row.status == "failed"
    assert row.error_info["reason"] == "wip_restore_conflict"
    assert row.error_info["wip_stash"] == sha
    assert pushes == []
    assert len(notices) == 1
    assert "wip_restore_conflict" in notices[0]
    assert (wt / "app.py").read_text() == "value = 3\n"
    assert not (wt / "new_test.py").exists()
    assert _git(wt, "rev-parse", "refs/stash") == sha


def test_no_stash_still_pushes(tmp_path, monkeypatch):
    wt = _worktree(tmp_path)
    pushes = []
    app, queue = _dispatch_app(tmp_path, monkeypatch, wt, pushes)
    queue.enqueue(_task("impl-clean"))

    with TestClient(app):
        app.state.try_push_next("implementer")

    assert len(pushes) == 1
    assert queue.get_task("impl-clean").error_info is None


def test_retry_restores_source_task_stash(tmp_path, monkeypatch):
    wt = _worktree(tmp_path)
    sha = _stash(wt, "impl-source")
    pushes = []
    app, queue = _dispatch_app(tmp_path, monkeypatch, wt, pushes)
    queue.enqueue(_task("impl-source"))
    queue.dequeue(role="implementer")
    queue.force_fail("impl-source", "quota hold", {"wip_stash": sha})
    queue.enqueue(_task("opaque-retry-1", {
        "prev_task_id": "impl-source", "original_task_id": "impl-source",
        "retry_of": "impl-source", "retry_attempt": 1}),
        ingress="retry.failed_task",
        _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)

    with TestClient(app):
        app.state.try_push_next("implementer")

    assert len(pushes) == 1
    assert (wt / "app.py").read_text() == "value = 2\n"
    assert (wt / "new_test.py").exists()
    assert queue.get_task("opaque-retry-1").error_info["wip_restored"] == sha


def test_retry_does_not_reapply_source_stash_already_restored(tmp_path, monkeypatch):
    wt = _worktree(tmp_path)
    sha = _stash(wt, "impl-source")
    pushes = []
    app, queue = _dispatch_app(tmp_path, monkeypatch, wt, pushes)
    queue.enqueue(_task("impl-source"))
    source = queue.dequeue(role="implementer")
    queue.annotate_error_info("impl-source", {"wip_stash": sha})
    server._restore_task_wip(queue, source, str(wt))
    _git(wt, "add", "app.py", "new_test.py")
    _git(wt, "commit", "-qm", "saved work")
    queue.force_fail("impl-source", "agent failed", {
        "wip_stash": sha, "wip_restored": sha,
    })
    queue.enqueue(_task("retry-impl-source-a1", {
        "prev_task_id": "impl-source", "original_task_id": "impl-source",
        "retry_of": "impl-source", "retry_attempt": 1,
    }), ingress="retry.failed_task",
        _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)

    with TestClient(app):
        app.state.try_push_next("implementer")

    assert len(pushes) == 1
    assert queue.get_task("retry-impl-source-a1").status == "in_progress"
    assert (wt / "app.py").read_text() == "value = 2\n"
    assert (wt / "new_test.py").exists()
    assert queue.get_task("retry-impl-source-a1").error_info is None


def test_restored_stash_is_not_applied_twice(tmp_path, monkeypatch):
    wt = _worktree(tmp_path)
    sha = _stash(wt, "impl-once")
    pushes = []
    app, queue = _dispatch_app(tmp_path, monkeypatch, wt, pushes)
    queue.enqueue(_task("impl-once"))
    queue.annotate_error_info("impl-once", {"wip_stash": sha})

    with TestClient(app):
        app.state.try_push_next("implementer")
        server._restore_task_wip(queue, queue.get_task("impl-once"), str(wt))

    assert len(pushes) == 1
    assert (wt / "app.py").read_text() == "value = 2\n"
    assert (wt / "new_test.py").exists()


def test_headless_dispatch_restores_before_worker_launch(tmp_path, monkeypatch, free_tcp_port):
    wt = _worktree(tmp_path)
    sha = _stash(wt, "impl-headless")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "role_agents": {"implementer": "codex", "reviewer": "claude", "tester": "script"},
        "worktrees": {"codex": str(wt)},
    }))
    db = str(tmp_path / "tasks.db")
    observed = []

    def prepare(*args, **kwargs):
        _git(wt, "reset", "--hard", "HEAD")
        _git(wt, "clean", "-fd")
        return _git(wt, "rev-parse", "HEAD")

    async def worker(*args, **kwargs):
        observed.append(((wt / "app.py").read_text(), (wt / "new_test.py").exists()))
        TaskQueue(db).submit_result("impl-headless", TaskResult(
            task_id="impl-headless", status="failed", summary="worker done"))

        class Process:
            returncode = 0
            pid = 839

            async def communicate(self):
                return b"", b""

            async def wait(self):
                return 0

            def kill(self):
                pass

            def terminate(self):
                pass

        return Process()

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setattr(server, "_prepare_worktree_for_task", prepare)
    monkeypatch.setattr(server, "_ensure_role_protocol", lambda *args, **kwargs: True)
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", worker)
    monkeypatch.setattr(server, "_dispatch_timeout_for_role", lambda *args, **kwargs: 5)
    monkeypatch.setattr(server.os, "killpg", lambda *args, **kwargs: None)
    app = server.create_app(db_path=db, pane_map={}, port=free_tcp_port,
                            state_path=str(state), project="demo",
                            watchdog_disabled=True, anomaly_disabled=True)
    queue = TaskQueue(db)
    queue.enqueue(_task("impl-headless"))
    queue.annotate_error_info("impl-headless", {"wip_stash": sha})

    with TestClient(app):
        task = queue.dequeue(role="implementer")
        asyncio.run(app.state.dispatch_task(task, "implementer"))

    assert observed == [("value = 2\n", True)]
