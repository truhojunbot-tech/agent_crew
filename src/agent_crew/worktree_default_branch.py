"""Resolve an origin default branch shared by CLI sync and server prep (#555)."""

import os
import subprocess


_DEFAULT_BRANCHES: dict[str, str] = {}


def remote_default_branch(worktree_path: str) -> str:
    """Return the first resolvable origin default branch, or an empty string."""
    key = os.path.realpath(worktree_path)
    cached = _DEFAULT_BRANCHES.get(key)
    if cached:
        return cached

    def resolves(ref: str) -> bool:
        result = subprocess.run(
            ["git", "-C", worktree_path, "rev-parse", "--verify", f"{ref}^{{commit}}"],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    head = subprocess.run(
        ["git", "-C", worktree_path, "symbolic-ref", "--quiet",
         "refs/remotes/origin/HEAD"],
        capture_output=True, text=True, timeout=30,
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
