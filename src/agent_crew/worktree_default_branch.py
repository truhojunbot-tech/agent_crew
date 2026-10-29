"""Resolve an origin default branch shared by CLI sync and server prep (#555)."""

import os
import subprocess
import time


_DEFAULT_BRANCHES: dict[str, str] = {}


def remote_default_branch(worktree_path: str, *, deadline: float | None = None) -> str:
    """Return the first resolvable origin default branch, or an empty string.

    ``deadline`` is a ``time.monotonic()`` instant bounding all probes together
    (#893); past it, ``subprocess.TimeoutExpired`` is raised instead of
    starting another 30 s probe.
    """
    key = os.path.realpath(worktree_path)
    cached = _DEFAULT_BRANCHES.get(key)
    if cached:
        return cached

    def probe_timeout() -> float:
        if deadline is None:
            return 30
        left = deadline - time.monotonic()
        if left <= 0:
            raise subprocess.TimeoutExpired("remote default branch probe", 0)
        return min(30, left)

    def resolves(ref: str) -> bool:
        result = subprocess.run(
            ["git", "-C", worktree_path, "rev-parse", "--verify", f"{ref}^{{commit}}"],
            capture_output=True, text=True, timeout=probe_timeout(),
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    head = subprocess.run(
        ["git", "-C", worktree_path, "symbolic-ref", "--quiet",
         "refs/remotes/origin/HEAD"],
        capture_output=True, text=True, timeout=probe_timeout(),
    )
    prefix = "refs/remotes/origin/"
    target = head.stdout.strip()
    if head.returncode == 0 and target.startswith(prefix):
        branch = target[len(prefix):]
        if branch and branch != "HEAD" and resolves(target):
            _DEFAULT_BRANCHES[key] = branch
            return branch

    for branch in ("main", "master"):
        if resolves(f"{prefix}{branch}"):
            _DEFAULT_BRANCHES[key] = branch
            return branch
    return ""
