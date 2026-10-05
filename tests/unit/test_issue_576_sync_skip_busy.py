"""CLI sync leaves a worktree alone while its server task is running (#576)."""

import io
import json
import subprocess
from urllib.error import URLError

import pytest

from agent_crew import cli


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout.strip()


def _worktrees(tmp_path):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)],
                   check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(seed)],
                   check=True, capture_output=True)
    _git(seed, "config", "user.name", "Test User")
    _git(seed, "config", "user.email", "test@example.invalid")
    (seed / "base.txt").write_text("base\n")
    _git(seed, "add", "base.txt")
    _git(seed, "commit", "-m", "base")
    _git(seed, "push", "origin", "main")
    base = _git(seed, "rev-parse", "HEAD")
    _git(seed, "checkout", "-b", "feature")
    (seed / "feature.txt").write_text("feature\n")
    _git(seed, "add", "feature.txt")
    _git(seed, "commit", "-m", "feature")
    _git(seed, "push", "origin", "feature")
    feature = _git(seed, "rev-parse", "HEAD")
    busy = tmp_path / "busy"
    idle = tmp_path / "idle"
    _git(seed, "worktree", "add", "--detach", str(busy), feature)
    _git(seed, "worktree", "add", "--detach", str(idle), feature)
    return busy, idle, base, feature


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_sync_skips_dispatched_busy_worktree_and_syncs_idle_one(
        tmp_path, monkeypatch, capsys):
    busy, idle, base, feature = _worktrees(tmp_path)
    dirty = busy / "unfinished.txt"
    dirty.write_text("leave this alone\n")
    monkeypatch.setattr(cli, "_fetch_tasks_by_status",
                        lambda port, status, project: [{"task_id": "review-active"}])

    def task_detail(req, timeout):
        assert req.get_header("X-agent-crew-project") == "demo"
        assert timeout == 2
        assert req.full_url.endswith("/tasks/review-active")
        return _Response(json.dumps({
            "status": "in_progress",
            "execution": {"dispatched_at": 1, "dispatch_agent": "claude"},
        }).encode())

    monkeypatch.setattr("urllib.request.urlopen", task_detail)
    landed = cli._sync_worktrees_to_main(
        {"claude": str(busy), "gemini": str(idle)},
        base_branch="main", port=8105, project="demo")

    assert _git(busy, "rev-parse", "HEAD") == feature
    assert dirty.read_text() == "leave this alone\n"
    assert _git(busy, "stash", "list") == ""
    assert _git(idle, "rev-parse", "HEAD") == base
    assert landed["claude"] == {"requested_ref": "origin/main", "actual_ref": None,
                                "sha": None, "status": "skipped_busy",
                                "task_id": "review-active"}
    assert landed["gemini"]["sha"] == base
    assert landed["gemini"]["status"] == "known"
    assert "Skipping busy worktree 'claude' (task review-active)." in capsys.readouterr().out


def test_sync_preserves_existing_behavior_when_server_unreachable(tmp_path, monkeypatch):
    busy, idle, base, _ = _worktrees(tmp_path)
    monkeypatch.setattr(cli, "_fetch_tasks_by_status",
                        lambda *args, **kwargs: (_ for _ in ()).throw(URLError("down")))

    landed = cli._sync_worktrees_to_main(
        {"claude": str(busy), "gemini": str(idle)},
        base_branch="main", port=8105, project="demo")

    assert _git(busy, "rev-parse", "HEAD") == base
    assert _git(idle, "rev-parse", "HEAD") == base
    assert {record["status"] for record in landed.values()} == {"known"}


@pytest.mark.parametrize("context, expected_agent", [
    ({}, "claude"),
    ({"agent_override": "gemini"}, "gemini"),
])
def test_claimed_before_dispatch_is_busy(tmp_path, monkeypatch, context, expected_agent):
    """The server pins a claimed task's worktree before dispatched_at exists."""
    busy, idle, base, feature = _worktrees(tmp_path)
    worktrees = {"claude": str(busy), "gemini": str(idle)}
    target = busy if expected_agent == "claude" else idle
    monkeypatch.setattr(cli, "_fetch_tasks_by_status",
                        lambda *args, **kwargs: [{"task_id": "review-claimed"}])
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: _Response(
        json.dumps({"task_id": "review-claimed", "task_type": "review",
                    "status": "in_progress", "context": context,
                    "execution": {"claimed_by_role": "reviewer", "dispatched_at": 0}}).encode()))

    landed = cli._sync_worktrees_to_main(
        worktrees, base_branch="main", port=8105, project="demo")

    assert _git(target, "rev-parse", "HEAD") == feature
    assert landed[expected_agent]["status"] == "skipped_busy"
    other = "gemini" if expected_agent == "claude" else "claude"
    assert landed[other]["sha"] == base


@pytest.mark.parametrize("tasks,detail", [
    ([{}], None),
    ([{"task_id": "finished"}], {"status": "completed", "execution": {}}),
    ([{"task_id": "unknown-agent"}], {"status": "in_progress",
                                    "execution": {"dispatched_at": 1,
                                                  "dispatch_agent": "other"}}),
])
def test_busy_lookup_filters_nonmatching_tasks(
        tmp_path, monkeypatch, tasks, detail):
    wt = tmp_path / "claude"
    wt.mkdir()
    monkeypatch.setattr(cli, "_fetch_tasks_by_status",
                        lambda *args, **kwargs: tasks)

    def task_detail(*_args, **_kwargs):
        assert detail is not None
        return _Response(json.dumps(detail).encode())

    monkeypatch.setattr("urllib.request.urlopen", task_detail)
    assert cli._busy_sync_worktrees(
        {"claude": str(wt)}, port=8105, project="demo") == {}
