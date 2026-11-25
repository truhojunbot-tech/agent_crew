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


def _rev(worktree_path: str, ref: str, timeout: float) -> str:
    result = subprocess.run(
        ["git", "-C", worktree_path, "rev-parse", "--verify", f"{ref}^{{commit}}"],
        capture_output=True, text=True, timeout=timeout,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def stale_base_gap(worktree_path: str, base: str, default: str, *,
                   timeout: float = 30) -> int:
    """How many commits ``origin/<base>`` trails ``origin/<default>`` by (#1065).

    Returns 0 unless ``base`` is *strictly* behind the repo default: both refs
    resolve, they differ, and ``origin/<base>`` is an ancestor of
    ``origin/<default>``. A branch that is ahead of or diverged from the
    default is not stale; neither is a branch origin does not have yet.
    """
    base = (base or "").strip()
    default = (default or "").strip()
    if not base or not default or base == default:
        return 0
    base_sha = _rev(worktree_path, f"refs/remotes/origin/{base}", timeout)
    default_sha = _rev(worktree_path, f"refs/remotes/origin/{default}", timeout)
    if not base_sha or not default_sha or base_sha == default_sha:
        return 0
    ancestor = subprocess.run(
        ["git", "-C", worktree_path, "merge-base", "--is-ancestor", base_sha, default_sha],
        capture_output=True, text=True, timeout=timeout,
    )
    if ancestor.returncode != 0:
        return 0
    count = subprocess.run(
        ["git", "-C", worktree_path, "rev-list", "--count", f"{base_sha}..{default_sha}"],
        capture_output=True, text=True, timeout=timeout,
    )
    try:
        return int(count.stdout.strip() or 0) if count.returncode == 0 else 0
    except ValueError:
        return 0
