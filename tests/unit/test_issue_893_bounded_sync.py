"""#893: a slow pre-enqueue worktree sync must not outlive the caller.

`crew run` syncs worktrees before it enqueues. An unbounded `git fetch` held
the client past its caller's 240 s timeout, so the dispatch was lost with no
task and no error. The sync is now bounded by a deadline; on expiry it warns
and returns so the caller still reaches enqueue.
"""
import subprocess
import time
from unittest.mock import MagicMock

from agent_crew.cli import _sync_worktrees_to_main


def _hanging_fetch(hang_s):
    def run(argv, **kwargs):
        if "fetch" in argv:
            timeout = kwargs.get("timeout")
            time.sleep(hang_s if timeout is None else min(hang_s, timeout))
            if timeout is not None and timeout < hang_s:
                raise subprocess.TimeoutExpired(argv, timeout)
        if "rev-parse" in argv:
            return MagicMock(returncode=0, stdout="a" * 40 + "\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")
    return run


def test_slow_fetch_is_bounded_and_reported(monkeypatch, tmp_path, capsys):
    wts = {}
    for role in ("implementer", "reviewer", "tester"):
        (tmp_path / role).mkdir()
        wts[role] = str(tmp_path / role)
    monkeypatch.setattr("agent_crew.cli.subprocess.run", _hanging_fetch(3.0))
    monkeypatch.setattr("agent_crew.cli._busy_sync_worktrees", lambda *a, **k: {})
    monkeypatch.setenv("AGENT_CREW_SYNC_TIMEOUT", "0.3")

    start = time.monotonic()
    bases = _sync_worktrees_to_main(wts, base_branch="main")
    elapsed = time.monotonic() - start

    assert elapsed < 1.5, f"sync ran {elapsed:.1f}s past a 0.3s deadline"
    assert set(bases) == set(wts)
    assert all(b["status"] == "timeout" and b["sha"] is None for b in bases.values())
    assert "timed out" in capsys.readouterr().out


def test_fast_sync_is_unaffected_by_the_deadline(monkeypatch, tmp_path):
    (tmp_path / "implementer").mkdir()
    monkeypatch.setattr("agent_crew.cli.subprocess.run", _hanging_fetch(0.0))
    monkeypatch.setattr("agent_crew.cli._busy_sync_worktrees", lambda *a, **k: {})
    monkeypatch.setenv("AGENT_CREW_SYNC_TIMEOUT", "5")

    bases = _sync_worktrees_to_main({"implementer": str(tmp_path / "implementer")},
                                    base_branch="main")
    assert bases["implementer"]["status"] == "known"


class _Resp:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def test_slow_busy_lookup_is_bounded_and_does_not_read_as_idle(monkeypatch, tmp_path):
    """Review of PR #902: the busy-task HTTP lookup ran before any deadline check."""
    import json
    import socket

    (tmp_path / "implementer").mkdir()
    git_calls = []

    def run(argv, **kwargs):
        git_calls.append(argv)
        return MagicMock(returncode=0, stdout="a" * 40 + "\n", stderr="")

    def slow_urlopen(req, timeout=None):
        time.sleep(min(0.35, timeout))
        if timeout < 0.35:
            raise socket.timeout("timed out")
        url = req.full_url if hasattr(req, "full_url") else req
        if "status=" in url:
            return _Resp(json.dumps([{"task_id": "t1"}]).encode())
        return _Resp(json.dumps({"status": "in_progress",
                                 "task_type": "implement"}).encode())

    monkeypatch.setattr("agent_crew.cli.subprocess.run", run)
    monkeypatch.setattr("urllib.request.urlopen", slow_urlopen)
    monkeypatch.setenv("AGENT_CREW_SYNC_TIMEOUT", "0.05")

    start = time.monotonic()
    bases = _sync_worktrees_to_main({"implementer": str(tmp_path / "implementer")},
                                    base_branch="main", port=8999, project="p")
    elapsed = time.monotonic() - start

    assert elapsed < 0.25, f"busy lookup ran {elapsed:.2f}s past a 0.05s deadline"
    assert bases["implementer"]["status"] == "timeout"
    # An unfinished lookup must not be treated as "not busy" and mutate the tree.
    assert git_calls == []


def test_slow_default_branch_probe_is_bounded(monkeypatch, tmp_path):
    """Review of PR #902: the fallback default-branch probes used fixed 30 s timeouts."""
    from agent_crew import worktree_default_branch

    (tmp_path / "implementer").mkdir()
    monkeypatch.setattr(worktree_default_branch, "_DEFAULT_BRANCHES", {})
    monkeypatch.setattr("agent_crew.cli._busy_sync_worktrees", lambda *a, **k: {})

    def run(argv, **kwargs):
        timeout = kwargs.get("timeout")
        if "checkout" in argv:
            return MagicMock(returncode=1, stdout="", stderr="no such ref")
        if "symbolic-ref" in argv or "--verify" in argv:
            time.sleep(min(0.35, timeout))
            if timeout < 0.35:
                raise subprocess.TimeoutExpired(argv, timeout)
            return MagicMock(returncode=1, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="a" * 40 + "\n", stderr="")

    # Both modules share the one subprocess module object.
    monkeypatch.setattr("agent_crew.cli.subprocess.run", run)
    monkeypatch.setenv("AGENT_CREW_SYNC_TIMEOUT", "0.05")

    start = time.monotonic()
    bases = _sync_worktrees_to_main({"implementer": str(tmp_path / "implementer")},
                                    base_branch="feature-not-on-origin")
    elapsed = time.monotonic() - start

    assert elapsed < 0.25, f"default-branch probe ran {elapsed:.2f}s past a 0.05s deadline"
    assert bases["implementer"]["status"] == "timeout"


def test_default_branch_probe_without_deadline_keeps_30s_timeout(monkeypatch, tmp_path):
    from agent_crew import worktree_default_branch

    monkeypatch.setattr(worktree_default_branch, "_DEFAULT_BRANCHES", {})
    seen = []

    def run(argv, **kwargs):
        seen.append(kwargs.get("timeout"))
        return MagicMock(returncode=0, stdout="refs/remotes/origin/main\n", stderr="")

    monkeypatch.setattr("agent_crew.worktree_default_branch.subprocess.run", run)
    assert worktree_default_branch.remote_default_branch(str(tmp_path)) == "main"
    assert seen and all(t == 30 for t in seen)
