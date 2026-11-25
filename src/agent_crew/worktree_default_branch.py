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


#: Environment variable naming the fleet-wide base branch. Read ONLY here
#: (#1070): every other module asks :func:`resolve_base_branch` instead.
ENV_MAIN_BRANCH = "AGENT_CREW_MAIN_BRANCH"
#: Step 4 of :func:`resolve_base_branch`: the last resort when nothing answers.
DEFAULT_BASE_BRANCH = "main"


def configured_base_branch() -> str:
    """Step 3 of :func:`resolve_base_branch`: ``AGENT_CREW_MAIN_BRANCH``, or ``""``."""
    return os.environ.get(ENV_MAIN_BRANCH, "").strip()


def fallback_base_branch() -> str:
    """Steps 3-4 of :func:`resolve_base_branch`: the base when no repo can answer.

    For callers that have no checkout at all (the queue, a cloud preflight).
    Anything holding a path must call :func:`resolve_base_branch` so the
    repo's own default is consulted first.
    """
    return configured_base_branch() or DEFAULT_BASE_BRANCH


def resolve_base_branch(context, repo_path: str = "", *,
                        deadline: float | None = None) -> str:
    """The one base-branch resolver (#1070).

    Order:

    1. the task context's ``base_branch``;
    2. the repository's own default (``origin/HEAD``, else ``origin/main`` or
       ``origin/master``, via :func:`remote_default_branch`) when ``repo_path``
       names a checkout;
    3. the fleet-wide ``AGENT_CREW_MAIN_BRANCH``;
    4. ``main``.

    A fleet constant used to sit at step 2. On a repo whose default is
    ``master`` that merged the test stage against a stale or missing ``main``
    (trader, 2026-11-25: three script tests FAILED with ``merge_conflict``).
    The constant is now the fallback for a repo whose default cannot be read,
    never the first answer. Git failures and an expired ``deadline`` fall
    through to steps 3-4 rather than raising; callers that need a hard refusal
    check :func:`remote_default_branch` themselves.
    """
    ctx = context if isinstance(context, dict) else {}
    declared = str(ctx.get("base_branch") or "").strip()
    if declared:
        return declared
    if repo_path:
        try:
            default = remote_default_branch(repo_path, deadline=deadline)
        except (OSError, subprocess.TimeoutExpired):
            default = ""
        if default:
            return default
    return fallback_base_branch()
