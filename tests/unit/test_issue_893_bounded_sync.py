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
