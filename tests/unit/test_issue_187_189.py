"""Tests for issues #187 (crew enqueue) and #189 (auto-SSH origin).

#187: ``crew enqueue`` lets an agent delegate a review/test task without
calling ``crew run`` (which always coerces task_type=implement and leaves
the downstream pane idle).

#189: ``create_worktrees`` rewrites HTTPS GitHub origin to SSH so that
agents whose shell wrappers strip git's credential helper chain (gemini-
cli most notably) can still ``git fetch`` against PR branches.
"""
import json
import subprocess
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

import agent_crew.cli as cli
import agent_crew.setup as setup_mod


# ---------------------------------------------------------------------------
# #187 — crew enqueue
# ---------------------------------------------------------------------------

def test_b187_enqueue_writes_review_task_type_via_http(tmp_path):
    """`crew enqueue review ...` POSTs task_type=review (not implement)."""
    state_dir = tmp_path / "proj"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(json.dumps({
        "db": str(state_dir / "tasks.db"),
        "port": 9999,
        "session": "x",
        "agents": ["claude"],
        "worktrees": {},
        "server_pid": 0,
    }))

    captured = {}

    def fake_urlopen(req, timeout=10):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        resp = MagicMock()
        resp.status = 201
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    runner = CliRunner()
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = runner.invoke(cli.crew, [
            "enqueue", "review", "Review PR #1666",
            "--project", "proj", "--base", str(tmp_path),
            "--branch", "agent/x/1665", "--pr", "1666",
            "--prev-task-id", "impl-abc12345",
        ])

    assert result.exit_code == 0, result.output
    assert captured["url"] == "http://127.0.0.1:9999/tasks"
    body = captured["body"]
    assert body["task_type"] == "review"
    assert body["branch"] == "agent/x/1665"
    assert body["context"]["pr_number"] == 1666
    assert body["context"]["prev_task_id"] == "impl-abc12345"
    assert body["task_id"].startswith("review")
    # Echo the task_id back to stdout
    assert body["task_id"] in result.output


def test_b187_enqueue_test_type(tmp_path):
    """`crew enqueue test ...` sends task_type=test."""
    state_dir = tmp_path / "proj"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(json.dumps({
        "db": str(state_dir / "tasks.db"),
        "port": 9999,
        "session": "x", "agents": ["claude"], "worktrees": {}, "server_pid": 0,
    }))

    captured = {}
    def fake_urlopen(req, timeout=10):
        captured["body"] = json.loads(req.data.decode())
        r = MagicMock(); r.status = 201
        r.__enter__ = MagicMock(return_value=r); r.__exit__ = MagicMock(return_value=False)
        return r

    runner = CliRunner()
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = runner.invoke(cli.crew, [
            "enqueue", "test", "Verify PR #1666",
            "--project", "proj", "--base", str(tmp_path),
            "--branch", "agent/x/1665", "--prev-task-id", "review-x",
        ])

    assert result.exit_code == 0, result.output
    assert captured["body"]["task_type"] == "test"
    assert captured["body"]["context"]["prev_task_id"] == "review-x"
    assert "pr_number" not in captured["body"]["context"]


def test_b187_enqueue_rejects_unknown_task_type():
    runner = CliRunner()
    result = runner.invoke(cli.crew, [
        "enqueue", "garbage", "x", "--project", "p", "--base", "/tmp",
    ])
    assert result.exit_code != 0
    assert "invalid choice" in result.output.lower() or \
           "task_type" in result.output.lower() or \
           "garbage" in result.output.lower()


# ---------------------------------------------------------------------------
# #189 — auto SSH origin
# ---------------------------------------------------------------------------

def _fake_subprocess_factory(remote_url: str, ssh_ok: bool):
    """Return a `subprocess.run` substitute that simulates a project repo."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        if cmd[:2] == ["git", "-C"] and "remote" in cmd and "get-url" in cmd:
            result.stdout = remote_url + "\n"
        elif cmd[:2] == ["git", "-C"] and "remote" in cmd and "set-url" in cmd:
            run.set_url_to = cmd[-1]
        elif cmd[0] == "ssh":
            result.returncode = 1  # ssh -T always exits 1
            result.stderr = (
                "Hi truhojun! You've successfully authenticated, but GitHub "
                "does not provide shell access.\n"
                if ssh_ok
                else "Permission denied (publickey).\n"
            )
        return result
    run.set_url_to = None  # type: ignore[attr-defined]
    run.calls = calls       # type: ignore[attr-defined]
    return run


def test_b189_https_origin_switches_to_ssh_when_ssh_works():
    fake_run = _fake_subprocess_factory(
        "https://github.com/truhojun/alpha_engine.git", ssh_ok=True,
    )
    with patch("agent_crew.setup.subprocess.run", side_effect=fake_run):
        setup_mod._convert_origin_to_ssh_if_safe("/fake/repo")
    assert fake_run.set_url_to == "git@github.com:truhojun/alpha_engine.git"


def test_b189_https_origin_kept_when_ssh_fails():
    fake_run = _fake_subprocess_factory(
        "https://github.com/truhojun/alpha_engine.git", ssh_ok=False,
    )
    with patch("agent_crew.setup.subprocess.run", side_effect=fake_run):
        setup_mod._convert_origin_to_ssh_if_safe("/fake/repo")
    assert fake_run.set_url_to is None


def test_b189_ssh_origin_left_alone():
    fake_run = _fake_subprocess_factory(
        "git@github.com:truhojun/alpha_engine.git", ssh_ok=True,
    )
    with patch("agent_crew.setup.subprocess.run", side_effect=fake_run):
        setup_mod._convert_origin_to_ssh_if_safe("/fake/repo")
    # Should not even probe SSH since URL didn't match HTTPS regex.
    assert fake_run.set_url_to is None
    ssh_calls = [c for c in fake_run.calls if c[0] == "ssh"]
    assert not ssh_calls


def test_b189_non_github_https_origin_left_alone():
    fake_run = _fake_subprocess_factory(
        "https://gitlab.com/x/y.git", ssh_ok=True,
    )
    with patch("agent_crew.setup.subprocess.run", side_effect=fake_run):
        setup_mod._convert_origin_to_ssh_if_safe("/fake/repo")
    assert fake_run.set_url_to is None
