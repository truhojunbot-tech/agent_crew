"""#838: only transient Git lock failures retry the pre-dispatch stash."""

import logging
import subprocess

import pytest

from agent_crew import server


class ReachedFetch(Exception):
    pass


def _run_prep(monkeypatch, stash_errors):
    attempts = []
    sleeps = []
    errors = iter(stash_errors)
    monkeypatch.setattr(server, "_unborn_head_ref", lambda _path: "")
    monkeypatch.setattr(server.time, "sleep", sleeps.append)

    def fake_run(command, **_kwargs):
        if command[3:6] == ["stash", "push", "-u"]:
            attempts.append(command)
            error = next(errors, "")
            return subprocess.CompletedProcess(command, 1 if error else 0, "", error)
        if command[3:5] == ["rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(command, 0, "true\n", "")
        if command[3:5] == ["fetch", "origin"]:
            raise ReachedFetch
        raise AssertionError(f"unexpected git command: {command}")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    return attempts, sleeps


def test_lock_error_retries_then_continues_to_fetch(monkeypatch, caplog):
    attempts, sleeps = _run_prep(monkeypatch, [
        "fatal: Unable to create '/wt/.git/index.lock': File exists", "",
    ])

    with caplog.at_level(logging.WARNING), pytest.raises(ReachedFetch):
        server._prepare_worktree_for_task_inner("/wt", "task-838", "main", "implementer")

    assert len(attempts) == 2
    assert sleeps == [0.5]
    assert "Unable to create" in caplog.text


def test_non_lock_error_raises_without_retry(monkeypatch, caplog):
    attempts, sleeps = _run_prep(monkeypatch, ["fatal: could not write index"])

    with caplog.at_level(logging.WARNING), pytest.raises(server.WorktreeUnhealthy):
        server._prepare_worktree_for_task_inner("/wt", "task-838", "main", "implementer")

    assert len(attempts) == 1
    assert sleeps == []
    assert "fatal: could not write index" in caplog.text


def test_lock_error_exhausts_three_attempts(monkeypatch, caplog):
    attempts, sleeps = _run_prep(monkeypatch, ["fatal: cannot lock ref 'HEAD'"] * 3)

    with caplog.at_level(logging.WARNING), pytest.raises(server.WorktreeUnhealthy):
        server._prepare_worktree_for_task_inner("/wt", "task-838", "main", "implementer")

    assert len(attempts) == 3
    assert sleeps == [0.5, 1.5]
    assert "cannot lock ref" in caplog.text


def test_logged_stderr_is_capped(monkeypatch, caplog):
    error = "fatal: " + "x" * 600
    _run_prep(monkeypatch, [error])

    with caplog.at_level(logging.WARNING), pytest.raises(server.WorktreeUnhealthy):
        server._prepare_worktree_for_task_inner("/wt", "task-838", "main", "implementer")

    assert caplog.records[0].getMessage().split("stderr=", 1)[1] == error[:500]
