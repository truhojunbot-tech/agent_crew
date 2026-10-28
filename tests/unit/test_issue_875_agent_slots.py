"""#875: N worktree slots per agent (AGENT_CREW_AGENT_SLOTS)."""

import asyncio
import json
import logging
import os
import subprocess
import threading
import time

import pytest
from fastapi.testclient import TestClient

import agent_crew.server as server
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


def _git_repo(path):
    path.mkdir(parents=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["git", "init", "-q"], ["git", "commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(cmd, cwd=path, check=True, capture_output=True, env=env)
    return str(path)


@pytest.fixture
def crew(tmp_path, monkeypatch):
    claude_wt = _git_repo(tmp_path / "wt" / "claude")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "project": "demo",
        "role_agents": {"implementer": "claude", "reviewer": "codex", "tester": "script"},
        "worktrees": {"claude": claude_wt, "codex": str(tmp_path / "wt" / "codex")},
    }))
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", "0.02")
    monkeypatch.delenv("AGENT_CREW_AGENT_SLOTS", raising=False)
    server._AGENT_SLOTS_PARSED.clear()
    app = server.create_app(
        db_path=str(tmp_path / "tasks.db"), state_path=str(state),
        watchdog_disabled=True, anomaly_disabled=True)
    started: dict[str, str] = {}
    release = threading.Event()

    async def fake_dispatch(task, role, dispatch_agent=None, dispatch_worktree=None):
        started[task.task_id] = (dispatch_worktree
                                 or app.state.resolve_dispatch_target(task, role)[1])
        while not release.is_set():
            await asyncio.sleep(0.01)

    app.state.dispatch_task = fake_dispatch
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    for i in (1, 2, 3):
        queue.enqueue(TaskRequest(task_id=f"impl-{i}", task_type="implement",
                                  description=f"task {i}", project="demo"))
    return app, queue, started, release, claude_wt


def _wait(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        time.sleep(0.01)
    return pred()


def test_two_claude_slots_run_two_implements_concurrently(crew, monkeypatch):
    app, queue, started, release, claude_wt = crew
    monkeypatch.setenv("AGENT_CREW_AGENT_SLOTS", '{"claude": 2}')
    with TestClient(app):
        try:
            assert _wait(lambda: len(started) == 2)
            time.sleep(0.2)   # several more ticks: the third must not start
            assert len(started) == 2
            assert sorted(started.values()) == [claude_wt, claude_wt + "-2"]
            assert os.path.exists(os.path.join(claude_wt + "-2", ".git"))
            assert app.state.dispatcher_active_workers == {"claude", "claude-2"}
            assert app.state.dispatcher_active_worktrees == {claude_wt, claude_wt + "-2"}
            third = next(t for t in ("impl-1", "impl-2", "impl-3") if t not in started)
            assert queue.get_task_status(third) == "pending"
        finally:
            release.set()


def test_slot_worktree_is_detached_from_slot_one_repo(crew, monkeypatch):
    app, _queue, started, release, claude_wt = crew
    monkeypatch.setenv("AGENT_CREW_AGENT_SLOTS", '{"claude": 2}')
    with TestClient(app):
        try:
            assert _wait(lambda: len(started) == 2)
        finally:
            release.set()
    slot = claude_wt + "-2"
    head = subprocess.run(["git", "-C", slot, "symbolic-ref", "-q", "HEAD"],
                          capture_output=True, text=True)
    assert head.returncode != 0   # detached
    listed = subprocess.run(["git", "-C", claude_wt, "worktree", "list", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    assert f"worktree {slot}" in listed


def test_default_config_dispatches_one_claude_task_as_before(crew):
    app, queue, started, release, claude_wt = crew
    with TestClient(app):
        try:
            assert _wait(lambda: len(started) == 1)
            time.sleep(0.2)
            assert list(started.values()) == [claude_wt]
            assert app.state.dispatcher_active_workers == {"claude"}
            assert not os.path.exists(claude_wt + "-2")
            assert sum(queue.get_task_status(f"impl-{i}") == "pending"
                       for i in (1, 2, 3)) == 2
        finally:
            release.set()


def test_invalid_env_falls_back_to_one_slot(crew, monkeypatch):
    app, _queue, started, release, claude_wt = crew
    monkeypatch.setenv("AGENT_CREW_AGENT_SLOTS", '{"claude": 0}')
    with TestClient(app):
        try:
            assert _wait(lambda: len(started) == 1)
            time.sleep(0.2)
            assert list(started.values()) == [claude_wt]
            assert not os.path.exists(claude_wt + "-2")
        finally:
            release.set()


@pytest.mark.parametrize("raw", ["nope", "[2]", '{"claude": 0}', '{"claude": "2"}',
                                 '{"claude": 2.0}', '{"claude": true}', '{"": 2}'])
def test_invalid_slot_config_is_one_for_every_agent_logged_once(raw, monkeypatch, caplog):
    server._AGENT_SLOTS_PARSED.clear()
    monkeypatch.setenv("AGENT_CREW_AGENT_SLOTS", raw)
    with caplog.at_level(logging.WARNING, logger=server.logger.name):
        assert server._agent_slot_counts() == {}
        assert server._agent_slot_counts() == {}
    assert sum("AGENT_CREW_AGENT_SLOTS" in r.getMessage() for r in caplog.records) == 1


def test_valid_slot_config_and_naming(monkeypatch):
    server._AGENT_SLOTS_PARSED.clear()
    monkeypatch.setenv("AGENT_CREW_AGENT_SLOTS", '{"Claude": 3, "codex": 1}')
    assert server._agent_slot_counts() == {"claude": 3, "codex": 1}
    monkeypatch.delenv("AGENT_CREW_AGENT_SLOTS")
    assert server._agent_slot_counts() == {}
    assert server._agent_slot_key("claude", 1) == "claude"
    assert server._agent_slot_key("claude", 2) == "claude-2"
    assert server._agent_slot_worktree("/w/demo/claude", 1) == "/w/demo/claude"
    assert server._agent_slot_worktree("/w/demo/claude/", 3) == "/w/demo/claude-3"
