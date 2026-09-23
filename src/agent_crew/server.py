import asyncio
import contextlib
import contextvars
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Callable, Literal, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from agent_crew import instructions
from agent_crew.port_validation import require_project_port
from agent_crew import claude_transcript as _claude_transcript
from agent_crew.anomaly import check_wrong_repo
from agent_crew import context_pack as _cpack
from agent_crew.memory import (
    MemoryProvider,
    MemoryRequest,
    NullMemoryProvider,
    shadow_retrieve_bounded,
    shadow_telemetry,
)
from agent_crew.context_identity import (
    append_attribution_jsonl,
    detect_context_compaction,
    extract_claude_session_id,
    record_context_event,
)
from agent_crew.fallback import is_rate_limit_error
from agent_crew.github import get_repo
from agent_crew.loop import _resolve_verdict
from agent_crew.pipeline import (
    auto_enqueue_fix as _pipeline_auto_enqueue_fix,
    auto_enqueue_review as _pipeline_auto_enqueue_review,
    auto_enqueue_test as _pipeline_auto_enqueue_test,
    auto_fallback_failed_task as _pipeline_auto_fallback_failed_task,
    hold_mismatched_pr_result,
    no_artifact_result,
    resume_tier3_gate as _resume_tier3_gate,
    review_publication_decision,
    stale_review_task_id,
    verify_implement_artifact,
)
from agent_crew import provenance as _prov
from agent_crew.protocol import (
    GateRequest, TaskRequest, TaskResult, RESULT_BRANCH_CONTEXT_KEY,
    RESULT_COMMIT_CONTEXT_KEY,
)
from agent_crew.queue import TaskAlreadyExistsError, TaskQueue, _ROLE_TO_TYPE, _TYPE_TO_ROLE, task_issue_number
from agent_crew.role_mapping import DEFAULT_ROLE_TO_AGENT, EXPLICIT_SOURCE, effective_role_mapping
from agent_crew.watch import active_tasks_for_issue
from agent_crew.testing_policy import (
    effective_scope as _effective_scope,
    load_scope as _load_test_scope,
    scope_fingerprint as _scope_fingerprint,
    test_stage_lock,
)
from agent_crew.telemetry_response import response_log_telemetry

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.DEBUG,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)


class ResolveBody(BaseModel):
    status: Literal["approved", "rejected"]


def _pane_alive_for_push(pane_id: str) -> bool:
    """Return True if the tmux pane exists and can receive a push.

    Uses ``tmux list-panes -t <pane_id>`` which exits non-zero if the pane is
    gone (session killed, window closed, pane closed after crash).
    """
    r = subprocess.run(
        ["tmux", "list-panes", "-t", pane_id],
        capture_output=True,
    )
    return r.returncode == 0


def _resolve_tmux_pane_target(target: str) -> str:
    """Resolve a send-keys target to tmux's canonical ``%pane_id`` form."""
    if re.fullmatch(r"%\d+", target or ""):
        return target
    result = subprocess.run(
        ["tmux", "display-message", "-t", target, "-p", "#{pane_id}"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _recorded_pane_ids(
    state_path: Optional[str], pane_map: Optional[dict],
) -> tuple[set[str], bool, str]:
    """Load durable pane ownership, including why a fallback was needed."""
    if state_path:
        try:
            with open(state_path) as state_file:
                state = json.load(state_file)
            pane_ids = state.get("pane_ids") if isinstance(state, dict) else None
            if isinstance(pane_ids, list):
                owned = {pane_id for pane_id in pane_ids if isinstance(pane_id, str) and pane_id}
                state_map = state.get("pane_map", {}) if isinstance(state, dict) else {}
                if isinstance(state_map, dict):
                    owned.update(v for v in state_map.values() if isinstance(v, str) and v)
                return owned, True, ""
        except (OSError, json.JSONDecodeError):
            reason = "state_unreadable"
        else:
            reason = "pane_ids_missing"
        return ({pane_id for pane_id in (pane_map or {}).values()
                 if isinstance(pane_id, str) and pane_id}, False, reason)
    # Embedded callers without a project state have no wider project boundary;
    # keep their explicit pane map as the ownership declaration. Crew servers
    # always pass state_path and therefore take the durable branch above.
    return ({pane_id for pane_id in (pane_map or {}).values()
             if isinstance(pane_id, str) and pane_id}, False, "state_path_missing")


# Per-pane snapshot of the previous capture, keyed by pane_id. Used by the
# default pane-busy probe to decide "did anything change since the last
# tick?". Tests inject their own busy_fn so this dict is only touched by the
# default path; pollution between tests is handled by `_reset_pane_busy_cache`.
_PANE_BUSY_LAST: dict[str, str] = {}


def _reset_pane_busy_cache() -> None:
    """Clear the per-pane diff cache. Test-only entry point."""
    _PANE_BUSY_LAST.clear()


_WORKTREE_SYNC_DISABLED = os.getenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "").lower() in (
    "1", "true", "yes",
)
_WORKTREE_MAIN_BRANCH = os.getenv("AGENT_CREW_MAIN_BRANCH", "main")


def _ensure_role_protocol(
    role: str, worktree_path: str, project: str, port_file: str, *, agent: str, port: int = 0,
) -> bool:
    """Ensure the role's worker contract survived worktree synchronisation (#353)."""
    relative = instructions.ROLE_FILES.get(role)
    if not relative:
        logger.error("dispatcher: no protocol file is defined for role=%s", role)
        return False
    expected = os.path.join(worktree_path, relative)
    if os.path.isfile(expected):
        return True
    try:
        # Production setup writes this first.  Keep recovery/test dispatches
        # self-contained: regenerating a protocol needs a port file, and the
        # server already owns the authoritative bound port.
        if not os.path.exists(port_file):
            require_project_port(port, project)
            with open(port_file, "w") as f:
                f.write(f"{port}\n")
        created = instructions.write(
            role, worktree_path, project, port_file, agent=agent, delivery="dispatcher",
        )
    except Exception:
        logger.exception(
            "dispatcher: could not regenerate missing protocol for role=%s worktree=%s",
            role, worktree_path,
        )
        return False
    if not os.path.isfile(created):
        logger.error("dispatcher: protocol write returned missing path %s", created)
        return False
    logger.warning("dispatcher: regenerated missing %s protocol at %s (#353)", role, created)
    return True


def _required_context_recalled_observation(pack) -> Optional[bool]:
    """Map Context Pack's existing state to quota-core's nullable evidence (#342)."""
    # No pack (or a healthy pack with no recalled items) provides no positive
    # retrieval observation: unknown, never false.  A built degraded pack
    # denotes observed incomplete/failed retrieval.
    if pack is None:
        return None
    if bool(pack.degraded):
        return False
    return True if getattr(pack, "items", ()) else None


def _resolve_pr_head_branch(pr_number: int, cwd: Optional[str] = None) -> Optional[str]:
    """Return the head ref name for a GitHub PR, or None on failure.

    ⛔Runs `gh` inside `cwd` — normally the worktree being prepared. Without
      it, `gh` resolves the repository from the SERVER process's working
      directory, which is the instance directory and not a checkout at all.
      It then answers about some other repository (or nothing), this function
      returns None, and the caller silently falls back to the task's base
      branch — so a reviewer asked to review a PR reads `main` instead and
      reports findings about code the PR does not contain. That is not
      hypothetical: it produced three consecutive "PR #251 is unchanged"
      reviews of a branch whose fix was already pushed.
    """
    try:
        r = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "headRefName",
             "-q", ".headRefName"],
            capture_output=True, text=True, timeout=15, cwd=cwd or None,
        )
        if r.returncode == 0:
            branch = r.stdout.strip()
            if branch:
                return branch
        logger.warning(
            f"_resolve_pr_head_branch: gh could not resolve PR #{pr_number} "
            f"(cwd={cwd or os.getcwd()!r}): {(r.stderr or r.stdout).strip()[:200]}"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"_resolve_pr_head_branch: PR #{pr_number} lookup failed: {e}")
    return None


#: Branch names agent_crew generates itself, and may therefore force-move.
_OWNED_TASK_BRANCH_RE = re.compile(r"\Aagent/[^/]{1,12}\Z")
_OWNED_SETUP_BRANCH_RE = re.compile(
    r"\Aagent/[^/]+/(?:claude|codex|gemini|implementer|reviewer|tester)\Z"
)
_OWNED_REVIEW_OR_TEST_BRANCH_RE = re.compile(r"\A(?:review|test)/[0-9a-f]{8}\Z")


def _agent_crew_owns_branch(branch: str) -> bool:
    """May agent_crew force-move this ref?

    ⛔The dispatcher's worktrees are `git worktree add` off the caller's clone,
      so `refs/heads/*` is ONE namespace shared with every sibling worktree and
      with the clone itself — only the branch a worktree currently has checked
      out is exclusive to it. `git checkout -B <name>` moves that shared ref for
      everybody.

      #280 measured the consequence: a `crew discuss --branch fix/…` round runs
      as `implementer`, hit `checkout -B fix/… origin/main`, and reset a
      developer's branch to main's tip three times inside one session — in a
      clone the dispatcher was never pointed at, while they were committing to
      it. Nothing was lost only because they had pushed first.

      So: only names agent_crew itself generates are safe to force-move.
      Anything else — `main`, a feature branch, anything a human named — gets a
      detached checkout, which touches no ref at all.
    """
    name = (branch or "").strip()
    return (
        bool(_OWNED_TASK_BRANCH_RE.match(name))
        or bool(_OWNED_SETUP_BRANCH_RE.match(name))
        or bool(_OWNED_REVIEW_OR_TEST_BRANCH_RE.match(name))
    )


def _branch_ref(worktree_path: str, ref: str) -> str:
    """Return ``ref``'s commit SHA, or ``""`` when it does not resolve."""
    result = subprocess.run(
        ["git", "-C", worktree_path, "rev-parse", "--verify", f"{ref}^{{commit}}"],
        capture_output=True, text=True, timeout=30,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _is_ancestor(worktree_path: str, older: str, newer: str) -> bool:
    """Whether ``older`` is reachable from ``newer``."""
    return subprocess.run(
        ["git", "-C", worktree_path, "merge-base", "--is-ancestor", older, newer],
        capture_output=True, text=True, timeout=30,
    ).returncode == 0


def _owned_branch_reset_is_safe(worktree_path: str, branch: str, main_branch: str) -> tuple[bool, str]:
    """Whether resetting an owned ref cannot discard a local-only commit."""
    local = _branch_ref(worktree_path, f"refs/heads/{branch}")
    if not local:
        return True, ""
    remote = _branch_ref(worktree_path, f"refs/remotes/origin/{branch}")
    comparison = remote or _branch_ref(worktree_path, f"refs/remotes/origin/{main_branch}")
    return (bool(comparison) and _is_ancestor(worktree_path, local, comparison), local)


def _checkout_detached(worktree_path: str, refs, *, what: str) -> bool:
    """Detach HEAD at the first of ``refs`` that resolves. True if one did.

    Detached because owning a branch here is the hazard itself: a worktree
    sitting ON a branch can move that shared ref later, by its own reset or by
    the agent's. Detached HEAD gives the task the same content with no ref to
    move.
    """
    tried = []
    for ref in refs:
        if not ref or ref in tried:
            continue
        tried.append(ref)
        r = subprocess.run(
            ["git", "-C", worktree_path, "checkout", "--detach", ref],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            logger.info(
                f"_prepare_worktree_for_task: {what} detached at {ref} — "
                f"agent_crew does not own that branch name, so the ref is left "
                f"where it is (#280). Push with `git push origin HEAD:<branch>`."
            )
            return True
        logger.warning(
            f"_prepare_worktree_for_task: {what} detach at {ref} failed: "
            f"{r.stderr.strip()}"
        )
    return False


#: A full git object id — 40 hex for sha1 repos, 64 for sha256. Deliberately not
#: a prefix match: abbreviations are ambiguous by construction, and no real pin
#: is one (`reviewed_sha` is written from `rev-parse HEAD`, which is always full).
_OBJECT_ID_RE = re.compile(r"\A[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")


def _object_id_or_empty(value) -> str:
    """``value`` as a commit id, or ``""`` if it does not look like one.

    ⛔Shape-check BEFORE probing, because probing is not validation.
      `git rev-parse --verify <x>^{commit}` resolves any revision expression —
      `origin/main`, `HEAD~1`, a tag, `@` — so an unrestricted `reviewed_sha` of
      `origin/main` would resolve, short-circuit the PR-head lookup, and
      silently prepare AND attribute a PR review to main (review of PR #287).

      Not a security boundary: `rev-parse` is not a shell, so nothing here is
      injectable. The damage is misattribution, which is precisely what the
      whole `reviewed_sha` contract exists to prevent.
    """
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    return candidate if _OBJECT_ID_RE.match(candidate) else ""


class WorktreePrepRefused(RuntimeError):
    #: Reason recorded on the task when this refusal stops a dispatch. It lives
    #: on the class so it travels with the category — both handlers used to
    #: hardcode `pr_head_unresolved`, so an unhealable worktree sent an operator
    #: looking for a PR that was never the problem, and a third refusal reason
    #: would have inherited the wrong label by default (review of PR #298).
    reason = "worktree_prep_refused"

    """Prep refused to hand this worktree to an agent.

    The shared base both push paths catch. A refusal means the worktree is not
    in a state where a dispatch could be trusted — the agent would produce work
    about something other than what it was asked about, and nothing downstream
    would say so.
    """


class WorktreeUnhealthy(WorktreePrepRefused):
    """The worktree's own HEAD does not resolve and could not be healed (#296)."""

    reason = "worktree_unhealthy"


class WorktreeTargetUnresolved(WorktreePrepRefused):
    """The task named a PR whose head could not be resolved (#289).

    ⛔Raised instead of falling back to `task.branch`. For a PR task that branch
      is the BASE branch — usually `main` — so the fallback produced a review of
      main that satisfied every identity guard: HEAD matched `reviewed_sha`, and
      `reviewed_sha` was a real commit. Nothing downstream could tell it from a
      real review, and its cost was attributed to a valid SHA under the wrong
      artifact. #250/#251 is the same class with production precedent.

      The asymmetry decides it: a deferred review is recoverable, a confident
      review of the wrong tree is not.
    """

    reason = "pr_head_unresolved"


def _unborn_head_ref(worktree_path: str) -> str:
    """The branch HEAD points at when that branch does not exist yet (#296).

    ``""`` unless the worktree is in exactly the reported shape: `rev-parse
    HEAD` fails, HEAD *is* a symbolic ref, and the branch it names does not
    resolve. That conjunction is what an interrupted ref update leaves behind.

    ⛔Deliberately narrow. "HEAD does not resolve" alone covers a directory that
      is not a repository, a git that is not installed, a transient failure —
      none of which this heals, and treating them as the same condition would
      make prep refuse in situations it has always survived. Precision about
      the failure mode is what lets the repair be safe.
    """
    def _git(*args, timeout=15):
        try:
            return subprocess.run(["git", "-C", worktree_path, *args],
                                  capture_output=True, text=True, timeout=timeout)
        except Exception:  # noqa: BLE001
            return None

    head = _git("rev-parse", "HEAD")
    if head is None or head.returncode == 0:
        return ""
    symbolic = _git("symbolic-ref", "-q", "HEAD")
    ref = symbolic.stdout.strip() if symbolic is not None and symbolic.returncode == 0 else ""
    if not ref:
        return ""
    existing = _git("rev-parse", "--verify", "-q", ref)
    if existing is not None and existing.returncode == 0 and existing.stdout.strip():
        # The ref resolves, so HEAD is broken for some other reason — a dangling
        # object, say. Not the unborn shape, and `update-ref` here would
        # force-move a live ref in the shared namespace (#280).
        return ""
    return ref


def _worktree_is_healthy(worktree_path: str) -> bool:
    """Does this worktree's HEAD resolve? (#296)

    The cheapest possible question, asked before anything else touches the
    directory. An interrupted ref update can leave HEAD pointing at a branch
    that was never created — an "unborn HEAD" — with the whole tree staged, and
    `stash`/`fetch`/`checkout` all behave differently against that.
    """
    try:
        r = subprocess.run(
            ["git", "-C", worktree_path, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:  # noqa: BLE001
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def _heal_unborn_head(worktree_path: str, main_branch: str, ref: str = "",
                      *, what: str) -> bool:
    """Bring an unborn HEAD back into existence. True if the worktree is usable.

    The recipe is `update-ref` then `reset --mixed`, validated on the live
    worktrees before #296 was filed:

    ⛔NEVER `checkout -B` or `reset --hard`. On an unborn HEAD with everything
      staged those are precisely the two commands that discard the staged
      content, and in a worktree broken by an interrupted task that content may
      be the only record of what it had done. `--mixed` unstages and leaves
      every file where it is.

    ⛔`update-ref` only ever CREATES here. The ref is read first and the heal is
      abandoned if it already resolves: writing a ref that exists would be a
      force-move in the namespace shared with the caller's clone and every
      sibling worktree, which is the thing #280 exists to stop.
    """
    if not ref:
        head_ref = subprocess.run(
            ["git", "-C", worktree_path, "symbolic-ref", "-q", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
        ref = head_ref.stdout.strip() if head_ref.returncode == 0 else ""
    if not ref:
        logger.error(
            f"{what}: HEAD does not resolve and is not a symbolic ref either — "
            f"this is not the unborn-HEAD shape #296 heals, and guessing at a "
            f"repair could destroy state. Refusing."
        )
        return False

    existing = subprocess.run(
        ["git", "-C", worktree_path, "rev-parse", "--verify", "-q", ref],
        capture_output=True, text=True, timeout=15,
    )
    if existing.returncode == 0 and existing.stdout.strip():
        logger.error(
            f"{what}: {ref} already resolves, so HEAD is broken for some other "
            f"reason. Refusing rather than force-moving a live ref (#280)."
        )
        return False

    target = subprocess.run(
        ["git", "-C", worktree_path, "rev-parse", "--verify",
         f"origin/{main_branch}^{{commit}}"],
        capture_output=True, text=True, timeout=30,
    )
    sha = target.stdout.strip() if target.returncode == 0 else ""
    if not sha:
        logger.error(
            f"{what}: cannot resolve origin/{main_branch} to heal {ref}. Refusing."
        )
        return False

    # Forensics BEFORE the repair: an interrupted task's staged content is the
    # only trace of what it had done, and #296 asks that it never be discarded
    # without a record of what it was.
    staged = subprocess.run(
        ["git", "-C", worktree_path, "diff", "--cached", "--name-only"],
        capture_output=True, text=True, timeout=30,
    )
    names = [n for n in (staged.stdout or "").splitlines() if n.strip()]
    logger.warning(
        f"{what}: HEAD does not resolve — {ref} is unborn, with {len(names)} "
        f"staged path(s). This is the interrupted-ref-update shape from #296. "
        f"Healing by creating {ref} at origin/{main_branch} ({sha[:12]}) and "
        f"unstaging with `reset --mixed`; no file is deleted. First staged "
        f"paths: {names[:5]}"
    )

    # ⛔Compare-and-swap, not check-then-write. The pre-check above is a fast,
    #   clearer diagnostic; it is NOT what makes this safe. `update-ref --stdin`
    #   with `create` verifies non-existence as part of the same operation, so a
    #   sibling worktree creating this branch between the check and the write
    #   loses nothing — the create fails and we refuse. A plain
    #   `update-ref <ref> <sha>` would overwrite whatever landed in that window,
    #   which is the shared-namespace force-move #280 forbids, reached by a race
    #   instead of by intent (review of PR #298).
    create = subprocess.run(
        ["git", "-C", worktree_path, "update-ref", "--stdin", "-z"],
        input=f"create {ref}\x00{sha}\x00", capture_output=True, text=True,
        timeout=60,
    )
    if create.returncode != 0:
        logger.error(
            f"{what}: could not create {ref} atomically — another worktree may "
            f"have created it first. Refusing rather than overwriting it (#280): "
            f"{create.stderr.strip()}"
        )
        return False
    reset = subprocess.run(
        ["git", "-C", worktree_path, "reset", "--mixed"],
        capture_output=True, text=True, timeout=60,
    )
    if reset.returncode != 0:
        logger.error(f"{what}: heal step 'reset' failed: {reset.stderr.strip()}")
        return False

    healed = _worktree_is_healthy(worktree_path)
    logger.warning(
        f"{what}: heal {'succeeded' if healed else 'FAILED'} — HEAD now "
        f"{'resolves' if healed else 'still does not resolve'} (#296)."
    )
    return healed


def _prepare_worktree_for_task(
    worktree_path: str,
    task_id: str,
    task_branch: str,
    role: str,
    task_context: Optional[dict] = None,
) -> str:
    """Sync worktree to origin and checkout the right branch before task dispatch.

    - All roles: stash local changes, fetch origin (catches stale worktrees that
      missed weeks of merged PRs, #141).
    - implementer: checkout a fresh branch per task from the task's configured
      base so each
      impl task starts clean and pushes to its own PR branch (#140).
    - reviewer/tester: checkout the task's PR branch from origin so reviews
      run against the actual changed code, not stale main (#141, #186).
      When task_context carries pr_number, the actual PR head ref is resolved
      via `gh pr view` so the worktree tracks the real PR branch rather than
      the base branch stored in task.branch.

    Failures are logged but never propagate — task dispatch continues even if
    the git prep encounters a transient error (e.g. merge conflict on stash pop).
    """
    try:
        return _prepare_worktree_for_task_inner(
            worktree_path, task_id, task_branch, role,
            task_context=task_context or {}) or ""
    except WorktreePrepRefused:
        # ⛔Not swallowed. Every other prep failure is survivable — a stash
        #   conflict, a slow fetch — and dispatch continues on a worktree that
        #   is merely stale. This one is not: continuing means reviewing the
        #   wrong artifact and saying nothing (#289).
        raise
    except Exception:
        logger.exception(
            f"_prepare_worktree_for_task: unexpected error for {role} "
            f"task_id={task_id} — continuing"
        )
        return ""


def _worktree_head(worktree_path: str) -> str:
    """The commit a worktree currently sits on, or "" if it cannot be read."""
    try:
        r = subprocess.run(["git", "-C", worktree_path, "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=15)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _prepare_worktree_for_task_inner(
    worktree_path: str,
    task_id: str,
    task_branch: str,
    role: str,
    task_context: Optional[dict] = None,
) -> None:
    """Inner (may raise). Wrapped by _prepare_worktree_for_task."""
    if task_context is None:
        task_context = {}
    # `main` is only a default.  A project can run on a long-lived integration
    # branch; dispatching its worker from origin/main silently makes it edit a
    # stale, different tree (#353).  The explicit context is produced by the
    # CLI and survives queue/restart; task.branch remains the useful fallback
    # for callers which do not supply one.
    main_branch = str(task_context.get("base_branch") or _WORKTREE_MAIN_BRANCH).strip()
    # #296: is this worktree even usable? Asked BEFORE any other git call,
    # because an interrupted ref update can leave HEAD pointing at a branch that
    # was never created, and `stash`/`fetch`/`checkout` all behave differently
    # against an unborn HEAD. Reported live: three role worktrees in that state
    # with 4,000+ files staged each, and nothing detected it.
    _unborn = _unborn_head_ref(worktree_path)
    if _unborn:
        _what = f"_prepare_worktree_for_task: {role} {task_id}"
        if not _heal_unborn_head(worktree_path, main_branch, _unborn, what=_what):
            raise WorktreeUnhealthy(
                f"{role} {task_id}: worktree {worktree_path} has no resolvable "
                f"HEAD and could not be healed. Refusing to dispatch into it "
                f"(#296)."
            )

    # Stash any leftover uncommitted changes so checkout doesn't fail.
    # timeout=30: git commands here are plain local operations that should
    # be near-instant. Without a timeout, a stuck git process (e.g. one
    # blocked in uninterruptible disk I/O — observed live on alpha_engine
    # 2026-08-27 during host-level swap pressure, where a fetch got stuck
    # in D-state) runs synchronously inside this async dispatch path and
    # freezes the entire event loop — including unrelated HTTP requests
    # like /health — for as long as the git process stays stuck, which a
    # signal-based timeout can't even interrupt once a process is truly in
    # D-state. This can't fix that specific case (nothing userspace can),
    # but it bounds every OTHER local git op here so one slow/stuck call
    # fails that single task instead of being able to hang indefinitely.
    subprocess.run(
        ["git", "-C", worktree_path, "stash", "push", "-u",
         "-m", f"agent_crew pre-{task_id[:8]}"],
        capture_output=True, text=True, timeout=30,
    )
    # Fetch all remote branches so the target ref is up to date.
    subprocess.run(
        ["git", "-C", worktree_path, "fetch", "origin", "--quiet"],
        capture_output=True, text=True,
        timeout=60,
    )

    if role == "implementer":
        # Fresh branch per task from the configured base (#140/#353). Use task.branch when
        # set (crew run --branch), otherwise derive from task_id.
        branch = task_branch if task_branch else f"agent/{task_id[:12]}"
        if not _agent_crew_owns_branch(branch):
            # #280: somebody else's branch name. Do not create it, do not move
            # it — start from its own remote tip so the task still sees the code
            # it was dispatched for, and fall back to main when there is no such
            # remote (a name that does not exist yet).
            _checkout_detached(
                worktree_path,
                [f"origin/{branch}", f"origin/{main_branch}"],
                what=f"implementer {task_id}",
            )
        else:
            reset_is_safe, local_sha = _owned_branch_reset_is_safe(
                worktree_path, branch, main_branch)
            if not reset_is_safe:
                logger.warning(
                    f"_prepare_worktree_for_task: refusing to reset owned branch {branch} "
                    f"at local-only commit {local_sha}; detaching at origin instead (#300)"
                )
                _checkout_detached(
                    worktree_path,
                    [f"origin/{branch}", f"origin/{main_branch}"],
                    what=f"implementer {task_id} preserving {branch}",
                )
            else:
                r = subprocess.run(
                    ["git", "-C", worktree_path, "checkout", "-B", branch,
                     f"origin/{main_branch}"],
                    capture_output=True, text=True, timeout=30,
                )
                if r.returncode != 0:
                    logger.warning(
                        f"_prepare_worktree_for_task: implementer checkout {branch} "
                        f"from origin/{main_branch} failed: {r.stderr.strip()}"
                    )
    else:
        # Reviewer/tester: checkout the PR branch from origin (#141, #186).
        # task.branch holds the base branch (e.g. main), not the PR head.
        # Resolve the actual PR head ref from pr_number when available so
        # the worktree always mirrors the real PR, not a stale base branch.
        pr_branch = task_branch  # fallback: base branch from task.branch
        pr_number = task_context.get("pr_number")
        # ⛔Skip the PR-head lookup when the task is already pinned. It is the
        #   only network call in prep, its answer would be discarded, and — worse
        #   — a failure would log "THIS MAY NOT BE THE PR'S CODE" about a task
        #   that is about to be prepared at exactly the commit it names. A
        #   misleading error is not free (#286 review).
        # #304 review (P1): `expected_head_sha` is the immutable head a requeued
        # review was CREATED for, and it outranks everything — including the
        # PR's current branch, which by definition may have moved again between
        # requeue and dispatch. Before this it had no production reader at all:
        # it was written onto the requeued task and prep consulted only
        # `reviewed_sha`, so the requeue resolved the moving branch and could
        # land on a third commit. That is the very substitution #304 exists to
        # prevent, reintroduced by its own remedy.
        _expected_head = _object_id_or_empty(task_context.get("expected_head_sha"))
        _pinned_sha = _expected_head or _object_id_or_empty(
            task_context.get("reviewed_sha"))
        if _expected_head:
            probe = subprocess.run(
                ["git", "-C", worktree_path, "rev-parse", "--verify",
                 f"{_expected_head}^{{commit}}"],
                capture_output=True, text=True, timeout=30,
            )
            if probe.returncode != 0 or not probe.stdout.strip():
                # ⛔Refuse, never fall back. #289's rule: a reviewer that cannot
                #   find its target stops rather than reviewing something else —
                #   and here "something else" is precisely the moved head this
                #   task was created to get away from.
                raise WorktreeTargetUnresolved(
                    f"{role} {task_id} expects head {_expected_head[:12]}, which "
                    f"does not resolve in this repository. Refusing to fall back "
                    f"to the PR's current branch: this review exists because that "
                    f"head moved (#304). Fetch the commit or requeue."
                )
        if pr_number and not _pinned_sha:
            resolved = _resolve_pr_head_branch(int(pr_number), cwd=worktree_path)
            if resolved:
                pr_branch = resolved
                logger.info(
                    f"_prepare_worktree_for_task: resolved PR #{pr_number} "
                    f"head → {pr_branch!r} for {role} {task_id}"
                )
            else:
                # ⛔REFUSE, not warn. This used to log "THIS MAY NOT BE THE PR'S
                #   CODE; treat any finding from this task as suspect" and carry
                #   on — which says plainly that logging was never the fix. The
                #   worktree is left exactly where it is (#289).
                raise WorktreeTargetUnresolved(
                    f"{role} {task_id} names PR #{pr_number} but its head could "
                    f"not be resolved, and there is no valid reviewed_sha pin. "
                    f"Refusing to fall back to task.branch={task_branch!r}, which "
                    f"for a PR task is the base branch (#289)."
                )

        target_ref = f"origin/{pr_branch}" if pr_branch else f"origin/{main_branch}"
        # #286: resolve to an exact object FIRST, then detach at it. Two
        # reasons, and the second is the one #286 is about:
        #
        #   * a symbolic ref keeps moving. `reviewed_sha` is read from this
        #     worktree straight after and becomes the revision key every
        #     downstream guard and cost record uses (#253), so what we check out
        #     has to be immutable at the moment we name it;
        #   * detached rather than `-B <local_branch>`: reviewer and tester
        #     never commit, so a branch buys them nothing and costs the
        #     guarantee. It also removes the last reviewer/tester write to the
        #     shared `refs/heads/*` namespace (#280) — the two constraints point
        #     the same way.
        # #286 review (P1): an existing `reviewed_sha` on the task outranks the
        # current PR ref, and is tried first.
        #
        # ⛔Prep runs more than once for a task — `_try_push_next` prepares and
        #   records the pin, then `_dispatch_task` prepares again — and without
        #   this the second run re-resolved the branch and overwrote the pin. A
        #   review queued at A and dispatched after the head moved to B silently
        #   reviewed and recorded B, destroying the durable commit identity the
        #   stale-review gate reads (#253). The property is idempotence:
        #   preparing the same task twice lands on the same commit, whatever the
        #   remote did in between.
        #
        # ⛔Reviewer/tester only. A fix task carries the `reviewed_sha` of the
        #   review it answers, so pinning the implementer to it would check out
        #   the code being fixed instead of the branch to fix it on.
        _pin = _pinned_sha
        _candidates = ([_pin] if _pin else []) + [target_ref, f"origin/{main_branch}"]
        _resolved = ""
        _resolved_from = ""
        for _ref in _candidates:
            probe = subprocess.run(
                ["git", "-C", worktree_path, "rev-parse", "--verify", f"{_ref}^{{commit}}"],
                capture_output=True, text=True, timeout=30,
            )
            if probe.returncode == 0 and probe.stdout.strip():
                _resolved = probe.stdout.strip()
                _resolved_from = _ref
                if _pin and _ref == _pin:
                    logger.info(
                        f"_prepare_worktree_for_task: {role} {task_id} honouring "
                        f"the recorded pin {_resolved[:12]} rather than re-resolving "
                        f"{target_ref} — preparing twice must not move the task (#286)."
                    )
                elif _ref != target_ref:
                    logger.warning(
                        f"_prepare_worktree_for_task: {role} could not resolve "
                        f"{target_ref}, fell back to {_ref} ({_resolved[:12]})"
                    )
                elif _pin:
                    # ⛔Loud: the task said it was about a commit this repo
                    #   cannot resolve (a force-push, or a SHA from elsewhere).
                    #   Preparing anyway is right — stranding the task helps
                    #   nobody — but the recorded identity is about to change,
                    #   and that must not happen quietly.
                    logger.warning(
                        f"_prepare_worktree_for_task: {role} {task_id} was pinned to "
                        f"{_pin[:12]}, which does not resolve here; prepared at "
                        f"{_resolved[:12]} from {target_ref} instead. The task's "
                        f"reviewed_sha will change (#286)."
                    )
                break
        # #301: REFUSE rather than review main under another branch's name.
        # ⛔The fallback below is `origin/<main>`, which always resolves — so a
        #   task naming a branch this repo does not have is silently prepared at
        #   main and reviewed, with the findings reported under that task's name.
        #   The warning above says so and nothing reads it.
        #
        # ⚠️Found while investigating #301, NOT the cause of it. That incident's
        #   four `exit_1` reviews were codex usage-limit exhaustion
        #   (`dispatch_reviewer.log`), and #301 was closed as an inaccurate
        #   diagnosis. This remains a real defect on its own merits — #289
        #   already refused exactly this for an unresolvable PR head — but it was
        #   latent there, not causal.
        #
        #   This is #289's rule reaching the case it did not name: a reviewer
        #   that cannot find its target must stop, not review something else.
        #   A resolved pin still wins (#286) — it IS the target, already known.
        _main_ref = f"origin/{main_branch}"
        if _resolved and _resolved_from == _main_ref and target_ref != _main_ref:
            raise WorktreeTargetUnresolved(
                f"{role} {task_id} names branch {target_ref!r}, which does not "
                f"resolve in this repository. Refusing to fall back to "
                f"{_main_ref} — a review prepared at main is a review of the "
                f"wrong code, reported under this task's name (#301). If the "
                f"branch lives in another repo, the task is on the wrong queue."
            )
        if not _resolved:
            logger.error(
                f"_prepare_worktree_for_task: {role} {task_id} could not resolve "
                f"{target_ref} or origin/{main_branch} to a commit — the worktree "
                f"is left where it was and reviewed_sha will describe THAT, not "
                f"the PR (#286)."
            )
        else:
            r = subprocess.run(
                ["git", "-C", worktree_path, "checkout", "--detach", _resolved],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                logger.error(
                    f"_prepare_worktree_for_task: {role} detach at {_resolved[:12]} "
                    f"failed: {r.stderr.strip()}"
                )
            else:
                logger.info(
                    f"_prepare_worktree_for_task: {role} {task_id} detached at "
                    f"{_resolved[:12]} from {target_ref} — pinned, so the agent "
                    f"must not re-fetch (#286)."
                )
    # #253: report the commit this worktree was actually prepared at, so a
    # finding can be attributed to a STATE rather than to a wall-clock moment.
    # Without it nothing downstream can tell "this review is about the current
    # head" from "this review is about three commits ago", and a fix task gets
    # created for work that already exists.
    return _worktree_head(worktree_path)


def _review_result_is_actionable(result) -> bool:
    """Did this review actually review anything?

    ⛔`_resolve_verdict` maps every non-completed status to `request_changes`,
      so a reviewer that crashed asks for changes nobody requested — and
      `build_feedback` fills the fix task with its bare header, because a review
      that did not run has no findings. Measured 2026-09-13 (#301): four `exit_1`
      reviews drove five implement dispatches in fifteen minutes against a
      deliverable that never changed.

      A skipped cascade is recoverable; the provider invocations those rounds
      spent are not. That asymmetry is the whole argument (#250).
    """
    return getattr(result, "status", None) in (None, "completed")


_DEFAULT_ROLE_TO_AGENT = dict(DEFAULT_ROLE_TO_AGENT)
_DEFAULT_AGENT_TO_ROLE = {v: k for k, v in _DEFAULT_ROLE_TO_AGENT.items()}


# Tags that mean "retryable in the next few minutes" (server-side load-shed).
#: Statuses a task can hold when a result still arrives afterwards. Both mean
#: "the dispatcher stopped watching", not "the work is over" (#265).
# #314 §4 P0(replay side-effect boundary): replay는 **durable cascade transition(successor enqueue)만**
# 재실행해야 한다. replay가 전체 submit_result를 다시 돌리면 non-idempotent side effect가 중복된다:
#   · PR review comment(gh, receipt 없음) · fallback escalation gate + telegram · queue push(_try_push_next/
#     _try_push_discuss가 다른 pending task를 claim/start). successor enqueue는 stable id로 멱등하지만
# 이 side effect들은 아니다. replay 동안 이 ContextVar를 True로 세우고, 각 side effect가 확인해 skip한다.
# (억제됐다 replay되는 result의 comment/escalation은 loss 가능하나, 리뷰어 판정상 duplication보다 허용됨.)
_REPLAYING = contextvars.ContextVar("agent_crew_replaying", default=False)

_LATE_RESULT_STATUSES = frozenset({"failed", "timed_out"})
# #314 §5: merge 자동 재시도 상한. 이 횟수 이상 실패(conflict/gh 실패 등)면 자동 재시도 중단 →
# escalation 대상(무한 재시도 금지). 비가역 상태(closed)는 횟수와 무관하게 즉시 재시도 안 함.
_MAX_MERGE_ATTEMPTS = 3

_TRANSIENT_RETRIABLE_TAGS = frozenset({
    "claude_429",
    "claude_throttle",
    "gemini_capacity",
    "gemini_resource_exhausted",
    "codex_capacity",
    "agy_timeout",
    "agy_subscriber_lag",
})
# Tags that mean "exhausted for hours+; retry is futile". Surface as a
# clear-reason failure instead.
_TRANSIENT_NONRETRIABLE_TAGS = frozenset({
    "gemini_quota_exhausted",
    "gemini_ineligible_tier",
    "agy_quota_exhausted",
})


def _detect_transient_error_in_log(
    log_path: str,
    tail_bytes: int = 16384,
    since_offset: int = 0,
) -> Optional[str]:
    """Scan the tail of a dispatch log for upstream errors worth distinguishing.

    ``dispatch_{role}.log`` is one continuously-appended file shared by every
    task dispatched for that role, not one file per task. A blind tail scan
    can therefore pick up an error signature left over from a *previous*
    task's failure (e.g. a quota message a few hundred bytes before EOF) and
    misattribute it to the current task, wrongly marking a retryable failure
    (or even a clean run) as the older task's non-retryable reason. Pass the
    file offset captured right after writing this task's ``TASK <id>``
    marker as ``since_offset`` so the scan never looks earlier than where
    this task's own output begins (#200).

    Returns one of:
      retryable (transient; requeue makes sense in a minute or two):
        - ``claude_429``                — Anthropic temp throttle (api_error_status:429)
        - ``claude_throttle``           — "Server is temporarily limiting requests"
        - ``gemini_capacity``           — google MODEL_CAPACITY_EXHAUSTED (preview)
        - ``gemini_resource_exhausted`` — generic 429 RESOURCE_EXHAUSTED
        - ``codex_capacity``            — openai "Selected model is at capacity" (#196)
        - ``agy_timeout``               — agy "Error: timeout waiting for response"
          (#199); backend model-call timeout, distinct from the account-level
          quota cap (agy_quota_exhausted below)
        - ``agy_subscriber_lag``        — agy "the connection to the agent was
          interrupted ... subscriber fell behind updates, stalled for Xs"
          (#205); a client-side streaming/backpressure hiccup between agy
          and its backend, distinct from a model-call timeout — previously
          fell through undetected to a bare ``exit_N`` with no retry
      non-retryable (clear reason; no point in immediate retry):
        - ``gemini_quota_exhausted``    — daily user quota hit; reset 2-3h away
        - ``gemini_ineligible_tier``    — oauth-personal serving-disabled (#195)
        - ``agy_quota_exhausted``       — agy "Individual quota reached" (#197);
          rolling per-account cap, log states its own reset countdown
      ``None`` if no transient signature is present.
    """
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - tail_bytes, since_offset)
            f.seek(min(start, size))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    # Order matters: more specific markers first (QUOTA before RESOURCE since
    # QUOTA_EXHAUSTED responses also contain "RESOURCE_EXHAUSTED").
    if "QUOTA_EXHAUSTED" in tail or "Your quota will reset" in tail:
        return "gemini_quota_exhausted"
    if "IneligibleTierError" in tail:
        return "gemini_ineligible_tier"
    if "Individual quota reached" in tail:
        return "agy_quota_exhausted"
    if '"api_error_status":429' in tail:
        return "claude_429"
    if "Server is temporarily limiting requests" in tail:
        return "claude_throttle"
    if "MODEL_CAPACITY_EXHAUSTED" in tail:
        return "gemini_capacity"
    if "RESOURCE_EXHAUSTED" in tail:
        return "gemini_resource_exhausted"
    if "Selected model is at capacity" in tail:
        return "codex_capacity"
    if "Error: timeout waiting for response" in tail:
        return "agy_timeout"
    if "subscriber fell behind updates" in tail:
        return "agy_subscriber_lag"
    return None


def _dispatch_timeout_for_role(role: str) -> float:
    """Hard wall-clock timeout (seconds) for a dispatched subprocess.

    ``implement`` tasks routinely run longer than review/test — they write
    code across a real codebase and run test suites, not just read and
    verdict — and a single shared 900s default was killing legitimately
    still-working (not stuck) implementer subprocesses with
    ``dispatcher_timeout`` (observed live on alpha_engine 2026-08-27: 3
    consecutive kills on tasks whose own dispatch log showed them still
    actively producing tool calls right up to the kill). ``implementer``
    gets a longer default; every other role keeps the original 900s.

    Both defaults remain overridable: ``AGENT_CREW_DISPATCH_TIMEOUT_IMPLEMENTER``
    for the implementer role specifically, else ``AGENT_CREW_DISPATCH_TIMEOUT``
    for that role or any other — so setting only the generic var still
    raises every role uniformly, matching pre-existing behavior for anyone
    already relying on it.
    """
    default = "1800" if role == "implementer" else "900"
    if role == "implementer":
        env_value = os.getenv("AGENT_CREW_DISPATCH_TIMEOUT_IMPLEMENTER")
        if env_value is not None:
            return float(env_value)
    return float(os.getenv("AGENT_CREW_DISPATCH_TIMEOUT", default))


#: Cap on the agy/Antigravity conversation the tester resumes with
#: `--continue` (#236). 0 disables.
#:
#: ⛔`_cap_gemini_session_size` below guards `~/.gemini/tmp/<proj>/chats`,
#:   which is the **gemini-cli** store. The tester runs `agy`, which keeps
#:   conversations in `~/.gemini/antigravity-cli/conversations/<id>.db`, so
#:   that guard never covered this path. #232 measured the consequence:
#:   alpha_engine's resumed conversation reached ~30k steps / 137 MB, every
#:   dispatch re-sent it, quota hit 429, and agy surfaced only the
#:   downstream `subscriber fell behind updates` mask.
AGY_CONTEXT_MAX_MB = float(os.getenv("AGENT_CREW_AGY_CONTEXT_MAX_MB", "64"))

#: Cap on the Claude Code session the worker resumes with `--continue` (#260).
#: 0 disables.
#:
#: ⛔#236/#238 bounded the agy store and nothing else, but `--continue` was
#:   unconditional for claude and `resume --last` unconditional for codex, so
#:   those sessions never rotated at all. Measured 2026-09-03: every crew
#:   worktree had exactly ONE session file since 2026-08-21, alpha_engine's at
#:   290 MB, and quota-ops sat above 900k cached tokens on 81 of 1095 turns.
#:   The file size is the store, not the context window — Claude Code compacts
#:   internally — but a store that never rotates is the thing that keeps the
#:   window pinned near its ceiling, and it is the signal we can actually see
#:   from outside the CLI.
CLAUDE_CONTEXT_MAX_MB = float(os.getenv("AGENT_CREW_CLAUDE_CONTEXT_MAX_MB", "64"))

#: Cap on the CONTEXT WINDOW a resumed Claude session re-reads each turn, in
#: tokens. **0 = off, and that default is deliberate** (#284).
#:
#: ⛔File size and window size are close to uncorrelated. Measured across every
#:   claude worktree on this host, 2026-09-10:
#:
#:       project              MB      window
#:       agent_council      0.13      62,025
#:       halla              0.97     231,590
#:       quota-core         3.61     575,398
#:       quota-ops          9.33     606,702
#:       agent_crew        17.18     517,710
#:       alpha_engine      32.53     363,824
#:
#:   quota-ops carries a LARGER window than alpha_engine from a file a third
#:   the size, and not one of the seven is near the 64 MB byte cap while four
#:   re-bill over 350k tokens every turn. Claude Code compacts the store
#:   internally, so bytes stop tracking the window that actually gets billed.
#:
#: ⛔Off by default because choosing the number is provider-economics policy,
#:   which belongs to the quota layer, not to the dispatcher. A 400k default
#:   would reset three of seven worktrees on their next dispatch, and
#:   alpha_engine was already back at 363k shortly after a rotation — so a low
#:   cap would thrash rather than protect. The measurement is recorded either
#:   way (see `claude_context_exceeds_cap`), so the decision has data.
CLAUDE_CONTEXT_MAX_TOKENS = int(os.getenv("AGENT_CREW_CLAUDE_CONTEXT_MAX_TOKENS", "0"))

#: How many rollout files `codex_session_for_cwd` will read before giving up.
#: Codex's store is date-partitioned and unbounded — 9,894 sessions on this
#: host — so the search walks newest-first and stops early. A miss returns "",
#: which the dispatcher treats as "no binding" and therefore as fresh.
CODEX_SESSION_SCAN_LIMIT = int(os.getenv("AGENT_CREW_CODEX_SESSION_SCAN", "400"))


def _codex_home(home=None):
    import pathlib

    return pathlib.Path(home) if home else pathlib.Path.home() / ".codex"


#: Cap on the codex rollout a worker resumes (#260 review). 0 disables.
#:
#: ⛔This became measurable only once #262 bound a resume to ONE session by id.
#:   While `resume --last` was global there was no per-worktree file to size,
#:   and that limitation was documented — the reviewer correctly spotted that
#:   the binding made it stale. Measured 2026-09-06 across 9,894 rollouts:
#:   median 48 KB, p99 0.4 MB, and alpha_engine's codex worktree holding
#:   356.6 / 180.6 / 130.3 / 114.7 MB files. 64 MB sits ~160x above p99, so it
#:   cannot fire on ordinary work and does catch those.
CODEX_CONTEXT_MAX_MB = float(os.getenv("AGENT_CREW_CODEX_CONTEXT_MAX_MB", "64"))


def codex_session_for_cwd(cwd: str, *, home=None, limit=None) -> str:
    """The newest Codex session id recorded for ``cwd``, or ``""``."""
    return _codex_session_and_path(cwd, home=home, limit=limit)[0]


def codex_session_size(cwd: str, *, home=None, limit=None) -> tuple:
    """``(bytes, session_id)`` for the rollout a codex resume would replay.

    The rollout file IS the conversation `codex exec resume <id>` replays, so
    its size is the same measurement `agy_conversation_size` and
    `claude_session_size` make for the other two providers.
    """
    session, path = _codex_session_and_path(cwd, home=home, limit=limit)
    if not session or path is None:
        return (0, "")
    try:
        return (path.stat().st_size, session)
    except OSError:
        return (0, session)


def codex_rollout_path(session_id: str, *, home=None, limit=None):
    """The rollout file for a specific session id, or ``None``.

    Filenames end in the session id, so this is a bounded newest-first walk of
    the same date hierarchy — no read of file contents needed.
    """
    if not session_id:
        return None
    budget = CODEX_SESSION_SCAN_LIMIT if limit is None else limit
    try:
        root = _codex_home(home) / "sessions"
        if not root.is_dir():
            return None
        seen = 0
        for day in _codex_day_dirs(root):
            for path in sorted(day.glob(f"rollout-*{session_id}.jsonl"), reverse=True):
                return path
            seen += 1
            if seen >= budget:
                return None
    except Exception:  # noqa: BLE001
        return None
    return None


def codex_session_size_for_id(session_id: str, *, home=None) -> int:
    """Bytes of the rollout a resume of ``session_id`` would replay."""
    path = codex_rollout_path(session_id, home=home)
    if path is None:
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def codex_context_exceeds_cap(cwd: str, max_mb=None, *, home=None,
                              session_id: str = "") -> tuple:
    """Is the rollout a codex resume would replay past the cap (#260 review)?

    Mirrors the agy and claude pairs so all three providers trip the same
    downstream path — a forced fresh context, with nothing on disk deleted.
    """
    cap = CODEX_CONTEXT_MAX_MB if max_mb is None else max_mb
    if session_id:
        # ⛔Measure the session that will ACTUALLY be resumed. The dispatcher
        #   prefers the durable `provider_session_id` over "newest for this
        #   cwd", so measuring the newest could clear an oversized stored
        #   session, or reset a perfectly good one because an unrelated newer
        #   rollout happened to be large (#260 review). The measured file and
        #   the resumed file have to be the same file.
        size, session = codex_session_size_for_id(session_id, home=home), session_id
    else:
        size, session = codex_session_size(cwd, home=home)
    info = {"bytes": size, "conversation_id": session, "cap_mb": cap,
            "provider": "codex"}
    if not cap or cap <= 0 or not size:
        return (False, info)
    return (size > cap * 1048576, info)


def _codex_day_dirs(root):
    """Yield codex's day directories newest-first, descending lazily.

    The store is `sessions/YYYY/MM/DD/`. A generator rather than a list so the
    caller can stop after the first directory that satisfies it — which is the
    common case, and the difference between listing three directories and
    walking the entire store.

    Falls back to yielding a level that has no subdirectories, so a flat or
    differently-shaped store still resolves rather than silently returning
    nothing.
    """
    def _subdirs(path):
        try:
            return sorted((d for d in path.iterdir() if d.is_dir()), reverse=True)
        except OSError:
            return []

    years = _subdirs(root)
    if not years:
        yield root
        return
    for year in years:
        months = _subdirs(year)
        if not months:
            yield year
            continue
        for month in months:
            days = _subdirs(month)
            if not days:
                yield month
                continue
            for day in days:
                yield day


def _codex_session_and_path(cwd: str, *, home=None, limit=None) -> tuple:
    """The newest Codex session id recorded for ``cwd``, or ``""``.

    ⛔`codex exec resume --last` is GLOBAL. Codex keys its rollout store by
      date, not by working directory, so "the most recent recorded session" is
      whatever ran last anywhere on the host. Measured 2026-09-04: 9,894
      sessions across many directories, the newest belonging to
      `worktrees/alpha_engine/codex` while this project's newest was two days
      older — so a resume dispatched for agent_crew would have attached
      alpha_engine's provider state while telemetry still reported agent_crew's
      logical context (#262). The task would still have succeeded, which is what
      made it invisible.

      Each rollout's first record is a `session_meta` carrying `cwd` and `id` —
      the per-worktree binding the CLI does not expose directly — and
      `codex exec resume <SESSION_ID>` targets it exactly.
    """
    import json as _json

    budget = CODEX_SESSION_SCAN_LIMIT if limit is None else limit
    try:
        root = _codex_home(home) / "sessions"
        if not root.is_dir() or not cwd:
            return ("", None)
        # ⛔Descend the date hierarchy lazily. `rglob("*")` materialises the
        #   WHOLE store before any budget applies — 9,894 sessions plus their
        #   directories on this host — so an unresolved lookup paid a full-tree
        #   traversal on every dispatch and every post-run capture, with the
        #   read budget bounding only the file reads that followed (review of
        #   PR #266). Filenames embed an ISO timestamp and the tree is
        #   YYYY/MM/DD, so sorting each level descending reaches the newest
        #   sessions after listing three directories.
        # ⛔Read as we descend, and stop at the first match. Collecting the
        #   budget's worth of paths BEFORE reading any of them meant a hit still
        #   walked far enough to gather 400 candidates — so the newest session,
        #   which is usually the first file in the newest directory, cost a
        #   descent through months of history anyway.
        read = 0
        for day in _codex_day_dirs(root):
            for path in sorted(day.glob("rollout-*.jsonl"), reverse=True):
                if read >= budget:
                    return ("", None)
                read += 1
                try:
                    with open(path, errors="replace") as fh:
                        first = fh.readline()
                    meta = (_json.loads(first) or {}).get("payload") or {}
                except Exception:  # noqa: BLE001
                    continue
                if meta.get("cwd") == cwd and meta.get("id"):
                    return (str(meta["id"]), path)
    except Exception:  # noqa: BLE001 — resolution must never break a dispatch
        return ("", None)
    return ("", None)


def _claude_home(home=None):
    return _claude_transcript.claude_home(home)


def claude_task_start_boundary(cwd: str, *, provider_session_id: str = "") -> dict:
    """Return the durable append boundary for a Claude task at dispatch.

    A resumed Claude session is an append-only JSONL file, so its exact byte
    size is the boundary between the prior task and this one. A fresh session
    has no known transcript identity yet; retaining an empty identity makes
    that unobservable state explicit instead of reading an unrelated session.
    """
    if not provider_session_id:
        return {
            "session_id": "",
            "offset": 0,
            "fresh_session_paths": _claude_transcript.claude_session_paths(
                cwd, home=_claude_home()),
        }
    path = _claude_transcript.claude_session_path(
        cwd, home=_claude_home(), session_id=provider_session_id)
    if path is None:
        return {"session_id": provider_session_id, "offset": 0}
    try:
        return {"session_id": path.stem, "offset": path.stat().st_size}
    except OSError:
        return {"session_id": path.stem, "offset": 0}


def claude_session_size(cwd: str, *, home=None) -> tuple:
    """``(bytes, session_id)`` for the Claude Code session bound to ``cwd``.

    Claude Code keys its transcripts by working directory, with `/`, `.` and
    `_` all folded to `-`:

        /home/u/.agent_crew/worktrees/quota-ops/claude
        → ~/.claude/projects/-home-u--agent-crew-worktrees-quota-ops-claude/

    The session `--continue` resumes is the most recently written `.jsonl` in
    that directory. ``(0, "")`` when anything is missing or unreadable —
    sizing must never break a dispatch.
    """
    return _claude_transcript.claude_session_size(cwd, home=_claude_home(home))


def _usage_context_tokens(usage) -> Optional[int]:
    """The window a turn re-read — cached + freshly cached + new input.

    ``None`` when there was nothing to read, ``0`` when the fields are present
    and sum to zero. ⛔The distinction is the whole point of the signature
    (review of PR #285): returning a bare `0` for both let the caller skip a
    genuine zero-token turn and report an OLDER, larger window as current,
    which with a token cap configured forces a context reset on the strength of
    a window that is no longer there.

    ⛔`output_tokens` is excluded, and does not count as "something to read"
      either. It is what the turn produced, not what it re-reads on the next
      one; including it would inflate the number the cap is compared against,
      and treating it as a measurement would make an output-only block look
      like a zero window.
    """
    return _claude_transcript.usage_context_tokens(usage)


def claude_context_tokens(cwd: str, *, home=None) -> tuple:
    """``(tokens, session_id)`` for the window `--continue` would resume (#284).

    Reads the LAST turn that carries a `message.usage` block. That number —
    `cache_read_input_tokens` plus the rest of the input side — is what gets
    re-billed on every subsequent turn, and it is the cost #260's byte cap
    cannot see: quota-ops sat at 601,674 tokens from a 9.33 MB file.

    ⛔Walks backwards from EOF in chunks rather than scanning the file. This
      runs on every dispatch, against stores that reached 365 MB on this host
      (#269); a full scan would move that cost into the dispatch path.

    ⛔``None`` means UNKNOWN — no store, nothing readable, or no usage block in
      the tail. ``0`` means measured and genuinely empty. #288 needs these
      apart: they are different facts, and a cohort that cannot tell an
      unmeasurable session from an empty one is wrong in a way nobody can
      detect afterwards. Sizing still never breaks a dispatch; it just says so
      now instead of returning a number it never took.
    """
    return _claude_transcript.claude_context_tokens(cwd, home=_claude_home(home))


def claude_context_exceeds_cap(cwd: str, max_mb=None, *, home=None,
                               max_tokens=None) -> tuple:
    """Is the Claude Code session `--continue` would resume past the cap (#260)?

    Returns ``(over, info)``. Mirrors `agy_context_exceeds_cap` so both
    providers trip the same downstream path — a forced fresh context, with
    nothing on disk deleted or mutated.
    """
    cap = CLAUDE_CONTEXT_MAX_MB if max_mb is None else max_mb
    token_cap = CLAUDE_CONTEXT_MAX_TOKENS if max_tokens is None else max_tokens
    size, session = claude_session_size(cwd, home=home)
    tokens, _ = claude_context_tokens(cwd, home=home)
    # ⛔The window is recorded whether or not a token cap is set. #284 exists
    #   because nobody could see this number; shipping the cap without the
    #   measurement would leave the same blind spot for whoever has to choose
    #   the threshold.
    info = {"bytes": size, "conversation_id": session, "cap_mb": cap,
            "context_tokens": tokens, "cap_tokens": token_cap,
            "tripped_by": "", "provider": "claude"}
    over_bytes = bool(cap and cap > 0 and size and size > cap * 1048576)
    # `tokens is not None` states the intent; it is NOT load-bearing here, and
    # mutation testing says so — swapping it for truthiness kills no test,
    # because `None` and `0` are both falsy and both correctly mean "not over".
    # Kept because the guard becomes real the moment the comparison gains a
    # lower bound or an `>=`, and because the distinction is the whole point of
    # #288 one line away in `info`. Recorded rather than dressed up as a fix.
    over_tokens = bool(token_cap and token_cap > 0 and tokens is not None
                       and tokens > token_cap)
    if over_bytes:
        info["tripped_by"] = "bytes"
    elif over_tokens:
        info["tripped_by"] = "tokens"
    return (over_bytes or over_tokens, info)


def _agy_home(home=None):
    import pathlib as _p
    return _p.Path(home) if home is not None else _p.Path.home() / ".gemini"


def agy_conversation_size(cwd: str, *, home=None) -> tuple:
    """``(bytes, conversation_id)`` for the agy conversation bound to ``cwd``.

    agy keeps a plain ``{cwd: conversation_id}`` map in
    ``antigravity-cli/cache/last_conversations.json``; that is the join key
    between a worktree and the conversation `--continue` would resume.

    Sums the ``.db`` plus its ``-wal``/``-shm`` siblings — a conversation
    being actively written holds real bytes in the WAL, and ignoring it
    under-reports exactly the case we care about. ``(0, "")`` when anything
    is missing or unreadable; this must never break dispatch.
    """
    import json as _json
    try:
        cache = _agy_home(home) / "antigravity-cli" / "cache" / "last_conversations.json"
        if not cache.exists():
            return (0, "")
        conv = (_json.loads(cache.read_text()) or {}).get(cwd) or ""
        if not conv:
            return (0, "")
        d = _agy_home(home) / "antigravity-cli" / "conversations"
        total = 0
        for suffix in ("", "-wal", "-shm"):
            f = d / f"{conv}.db{suffix}"
            if f.exists():
                total += f.stat().st_size
        return (total, conv)
    except Exception:  # noqa: BLE001 — sizing must never break a dispatch
        return (0, "")


def agy_context_exceeds_cap(cwd: str, max_mb=None, *, home=None) -> tuple:
    """Is the conversation `--continue` would resume past the cap (#236)?

    Returns ``(exceeded, info)``. ⛔Fail-soft in the *resume* direction: if
    the size cannot be read we report False. Wrongly forcing a reset throws
    away a healthy conversation; wrongly resuming is merely the status quo
    that this cap exists to bound.
    """
    cap = AGY_CONTEXT_MAX_MB if max_mb is None else float(max_mb)
    info = {"bytes": 0, "conversation_id": "", "cap_mb": cap}
    if cap <= 0:
        return (False, info)
    try:
        size, conv = agy_conversation_size(cwd, home=home)
    except Exception:  # noqa: BLE001
        return (False, info)
    info["bytes"], info["conversation_id"] = size, conv
    return (size > cap * 1024 * 1024, info)


#: agy surfaces only the downstream symptom; the 429 stays in its own log.
_AGY_QUOTA_RE = re.compile(r"RESOURCE_EXHAUSTED|Individual quota reached", re.I)
_AGY_LAG_RE = re.compile(r"subscriber fell behind updates", re.I)


def agy_quota_correlated(since: float, until: float, *, home=None) -> bool:
    """Did a 429 precede a subscriber-lag kill inside this task's window?

    #232 measured that where both signals appear, the quota error comes
    first in 1,412 of 1,442 cases (98%) — the lag is the mask, not the
    cause. agent_crew never sees the 429 because it lives only in agy's own
    ``antigravity-cli/log/cli-*.log``.

    ⚠️Deliberately conservative on three axes, because a false "quota"
      verdict would silently strip a genuinely retriable failure of its
      retries:
      - only logs modified inside ``[since, until]`` are considered, so
        another task's quota failure is never attributed to this one;
      - the 429 must appear *before* the lag line in the same file;
      - anything unreadable returns False, preserving today's behaviour.
    """
    try:
        d = _agy_home(home) / "antigravity-cli" / "log"
        if not d.is_dir():
            return False
        for f in d.glob("cli-*.log"):
            try:
                mtime = f.stat().st_mtime
                if not (since <= mtime <= until):
                    continue
                text = f.read_text(errors="replace")
            except Exception:  # noqa: BLE001
                continue
            lag = _AGY_LAG_RE.search(text)
            if not lag:
                continue
            quota = _AGY_QUOTA_RE.search(text)
            if quota and quota.start() < lag.start():
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


def _cap_gemini_session_size(cwd: str, max_mb: int = 50) -> None:
    """Archive gemini-cli session files larger than ``max_mb`` for ``cwd``.

    Without this, ``gemini -p --resume latest`` will silently re-load a
    multi-hundred-MB session jsonl on every dispatch and blow past gemini's
    1M input-token limit, causing every tester task to fail with
    `DONE (0 turns)` before the agent ever sees the prompt. Once at the cap
    the file is moved to ``chats/_archive/`` (reversible) and gemini starts
    a fresh session on the next launch.
    """
    import pathlib
    try:
        projects_path = pathlib.Path.home() / ".gemini" / "projects.json"
        if not projects_path.exists():
            return
        with open(projects_path) as f:
            projects = json.load(f).get("projects", {})
        cwd_real = os.path.realpath(cwd)
        project_dir = projects.get(cwd) or projects.get(cwd_real)
        if not project_dir:
            return
        chats_dir = pathlib.Path.home() / ".gemini" / "tmp" / project_dir / "chats"
        if not chats_dir.is_dir():
            return
        archive_dir = chats_dir / "_archive"
        threshold = max_mb * 1024 * 1024
        for sess in chats_dir.glob("session-*.jsonl"):
            try:
                size = sess.stat().st_size
            except OSError:
                continue
            if size <= threshold:
                continue
            try:
                archive_dir.mkdir(exist_ok=True)
                sess.rename(archive_dir / sess.name)
                logger.warning(
                    f"_cap_gemini_session_size: archived {sess.name} "
                    f"({size // (1024 * 1024)}MB > {max_mb}MB cap) "
                    f"from {chats_dir}"
                )
            except OSError:
                logger.exception(
                    f"_cap_gemini_session_size: failed to archive {sess}"
                )
    except Exception:
        logger.exception("_cap_gemini_session_size: unexpected error")


def _rotate_log_if_oversized(path: str, max_mb: int, keep: int = 3) -> None:
    """Rotate ``path`` to ``path.1`` (and shift older files up) when it
    exceeds ``max_mb``. Keeps at most ``keep`` numbered rotations on disk;
    the oldest is dropped.

    Cheap to call per dispatch — os.stat is O(1); rotation only fires when
    the threshold is actually crossed (#193). Best-effort: silently skips
    on errors so a rotation hiccup doesn't take down the dispatcher.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size <= max_mb * 1024 * 1024:
        return
    try:
        # Slide path.(keep-1) → drop; path.(keep-2) → path.(keep-1); ...
        oldest = f"{path}.{keep}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for i in range(keep - 1, 0, -1):
            src = f"{path}.{i}"
            dst = f"{path}.{i + 1}"
            if os.path.exists(src):
                os.rename(src, dst)
        os.rename(path, f"{path}.1")
        logger.info(
            "log rotation: %s (%dMB > %dMB cap) → %s.1",
            path, size // (1024 * 1024), max_mb, path,
        )
    except OSError:
        logger.exception("log rotation failed for %s", path)


def _load_worktree_map(state_path: Optional[str]) -> dict[str, str]:
    """Derive {role: worktree_path} from state.json.

    Prefers the explicit ``roles`` list (new schema, supports same agent on
    multiple roles like claude implementer + claude reviewer). Falls back to
    the legacy ``worktrees: {agent: path}`` map with hardcoded role mapping
    when ``roles`` is absent.
    """
    if not state_path or not os.path.exists(state_path):
        logger.warning(f"_load_worktree_map: state_path={state_path!r} missing → worktree_map={{}}")
        return {}
    try:
        with open(state_path) as f:
            state = json.load(f)
        # New schema: ``roles`` stores existing worktrees.  A later explicit
        # provider assignment may re-key those worktrees by provider.
        roles_list = state.get("roles")
        if roles_list:
            by_role = {
                r["role"]: r["worktree"]
                for r in roles_list
                if isinstance(r, dict) and r.get("role") and r.get("worktree")
            }
            mapping, source = effective_role_mapping(state, project=state.get("project", "unknown"))
            if source == EXPLICIT_SOURCE:
                by_agent = {
                    r.get("agent"): r.get("worktree")
                    for r in roles_list
                    if isinstance(r, dict) and r.get("agent") and r.get("worktree")
                }
                worktrees = state.get("worktrees", {})
                if isinstance(worktrees, dict):
                    by_agent.update({agent: path for agent, path in worktrees.items() if agent and path})
                result = {}
                for role, agent in mapping.items():
                    path = by_agent.get(agent)
                    if path:
                        result[role] = path
                    else:
                        logger.warning(
                            "_load_worktree_map: explicit role %s uses agent %s with no worktree; omitting role",
                            role, agent,
                        )
            else:
                result = by_role
            logger.info(f"_load_worktree_map: state_path={state_path!r} (roles) → worktree_map={result}")
            return result
        # Legacy schema: agent-keyed worktrees + default role mapping.
        worktrees = state.get("worktrees", {})
        if not isinstance(worktrees, dict):
            worktrees = {}
        result = {}
        mapping, source = effective_role_mapping(state, project=state.get("project", "unknown"))
        if source == EXPLICIT_SOURCE:
            for role, agent in mapping.items():
                path = worktrees.get(agent)
                if path:
                    result[role] = path
                else:
                    logger.warning(
                        "_load_worktree_map: explicit role %s uses agent %s with no worktree; omitting role",
                        role, agent,
                    )
        else:
            for agent, path in worktrees.items():
                role = _DEFAULT_AGENT_TO_ROLE.get(agent)
                if role and path:
                    result[role] = path
        logger.info(f"_load_worktree_map: state_path={state_path!r} (legacy) → worktree_map={result}")
        return result
    except Exception:
        logger.exception("_load_worktree_map: failed to read state.json")
        return {}


def _load_role_to_agent_with_source(state_path: Optional[str]) -> tuple[dict[str, str], str]:
    """Load the effective provider-neutral mapping and its source label."""
    if not state_path or not os.path.exists(state_path):
        return effective_role_mapping(None)
    try:
        with open(state_path) as f:
            state = json.load(f)
        return effective_role_mapping(state, project=state.get("project", "unknown"))
    except Exception:
        logger.exception("_load_role_to_agent: failed to read state.json")
        return effective_role_mapping(None)


def _load_role_to_agent(state_path: Optional[str]) -> dict[str, str]:
    """Load the effective mapping, retaining the older helper shape."""
    return _load_role_to_agent_with_source(state_path)[0]


_THINKING_TAIL_LINES = 10
_THINKING_RE = re.compile(r"esc to interrupt|↓\s*[\d,.]+[kKmM]?\s*tokens", re.IGNORECASE)


def _pane_is_thinking(capture: str) -> bool:
    """Return True when the last visible lines contain active LLM generation markers.
    Only the bottom _THINKING_TAIL_LINES lines are checked so scrolled-away
    past-tense thinking output does not trigger false positives (#138).
    """
    tail = "\n".join(capture.splitlines()[-_THINKING_TAIL_LINES:])
    return bool(_THINKING_RE.search(tail))


def _pane_has_usage_limit(pane_id: str) -> bool:
    """Return True when the pane capture contains a rate-limit / usage-limit
    message, meaning the agent is blocked and can't accept new tasks (#151)."""
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane_id],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return False
        return is_rate_limit_error(r.stdout)
    except Exception:
        return False


# Visible strings in pane content that indicate an agent CLI is running.
# Must be lowercase for case-insensitive matching.
_AGENT_CLI_INDICATORS: tuple[str, ...] = (
    "bypass permissions",   # claude --dangerously-skip-permissions
    "skip permissions",     # claude
    "claude code",          # claude code header
    "gemini",               # gemini CLI footer
    "yolo",                 # gemini --approval-mode yolo
    "codex>",               # codex interactive prompt
    "enter your task",      # codex ready state
)




def _pane_has_bash_prompt(pane_id: str) -> bool:
    """Return True when the pane appears to be at a bare shell prompt,
    indicating the agent CLI has crashed or never started (#158).

    Heuristic: the last non-empty line ends with ``$`` or ``❯`` (typical
    bash/zsh prompts) AND the full capture contains no known agent CLI
    ready-indicators. Using both conditions avoids false positives from
    pane content that incidentally contains a ``$`` (e.g. shell variables
    visible in agent output).

    Returns False on any tmux error — we must never block a push based on
    an inconclusive probe.
    """
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane_id],
            capture_output=True, text=True,
        )
    except Exception:
        return False
    if r.returncode != 0:
        return False
    content = r.stdout
    # If any agent CLI indicator is visible, the CLI is running — not bash.
    if any(ind in content.lower() for ind in _AGENT_CLI_INDICATORS):
        return False
    # Check that the last non-empty line looks like a shell prompt.
    last_line = content.rstrip().rsplit("\n", 1)[-1] if content.strip() else ""
    # #173: also detect bash multi-line continuation prompt (>) which occurs
    # when partial text (e.g. a REMINDER block) is injected into bash causing
    # a syntax error and leaving the shell stuck in multi-line input mode.
    return bool(re.search(r"\$\s*$|❯\s*$|^>\s*$", last_line))


#: Foreground commands that mean the agent CLI exited (#195 crash signature).
_DEAD_PANE_COMMANDS = {"bash", "sh", "zsh", "fish", "dash"}


def _pane_liveness(pane_id: str) -> str:
    """``alive`` | ``dead`` | ``unknown`` for the process in ``pane_id`` (#231).

    Silence is not death. A pane running a full test suite produces no
    capture changes for minutes, which `_pane_is_busy` reports as idle; this
    is the second opinion the watchdog consults before destroying the task.
    Conservative by design — anything that is not a shell counts as alive,
    because a wrong "dead" verdict costs real work.
    """
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-t", pane_id, "-p",
             "#{pane_current_command}"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return "unknown"
    if r.returncode != 0:
        return "unknown"
    cmd = r.stdout.strip()
    if not cmd:
        return "unknown"
    return "dead" if cmd in _DEAD_PANE_COMMANDS else "alive"


def _pane_is_busy(pane_id: str) -> bool:
    """Return True if the pane is actively processing.

    Two complementary signals (either is sufficient):
    1. Content diff — the capture changed since the previous call (spinner
       ticking, output streaming in, token counter incrementing).
    2. Thinking markers — the visible bottom lines contain live-only
       indicators such as ``esc to interrupt`` that are cleared from the
       terminal as soon as the model stops generating. This catches long
       silent thinking phases where the capture is momentarily static (#138).

    Edge cases:
    - First call for a pane has no prior snapshot → content diff is False but
      thinking markers still apply. If the pane really just received a task
      and is processing, the thinking signal fires.
    - tmux capture-pane failing returns False — the watchdog must never
      crash on a transient pane-probe error.
    """
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane_id],
            capture_output=True, text=True,
        )
    except Exception:
        return False
    if r.returncode != 0:
        return False
    current = r.stdout
    prev = _PANE_BUSY_LAST.get(pane_id)
    _PANE_BUSY_LAST[pane_id] = current
    return (prev is not None and current != prev) or _pane_is_thinking(current)


_TOKEN_CLEAR_THRESHOLD = int(os.getenv("AGENT_CREW_TOKEN_CLEAR_THRESHOLD", "200000"))
_TOKEN_COUNT_RE = re.compile(r"save\s+([\d,]+(?:\.\d+)?[kKmM]?)\s+tokens", re.IGNORECASE)


def _pane_token_count(pane_id: str) -> Optional[int]:
    """The token count hinted in the pane status bar, or ``None`` if absent.

    Claude Code shows "new task? /clear to save 544.1k tokens" when context is
    large, and #133 parsed that hint to detect saturation.

    ⛔Returns ``None``, not ``0``, when the hint is not on screen. It returned
      ``0`` until #292, which every caller read as "well under threshold" — and
      the hint is usually NOT on screen: `capture-pane` without `-S` sees only
      the visible rows, and the footer renders only in some UI states. Measured
      2026-09-13 with the threshold at 200,000, five of seven worktrees were
      over the ceiling this mechanism exists to enforce (786,552 / 664,792 /
      606,702 / 575,398 / 231,590) while neither readable pane showed a hint.

      This is now a FALLBACK for providers with no transcript to read. Prefer
      `_context_token_count`.
    """
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane_id],
            capture_output=True, text=True,
        )
    except Exception:
        return None
    m = _TOKEN_COUNT_RE.search(r.stdout or "")
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    if raw.lower().endswith("k"):
        return int(float(raw[:-1]) * 1_000)
    if raw.lower().endswith("m"):
        return int(float(raw[:-1]) * 1_000_000)
    return int(float(raw))


def _roles_for_agent(agent: str, role_to_agent=None) -> list:
    """Every role the live configuration assigns to ``agent`` (#292/#299).

    One lookup, shared by the worktree resolver and the auto-clear event.
    ⛔Two copies of "which role is this agent" would drift, and the drift would
      be invisible until one of them described a deployment that does not
      exist — which is exactly how both of these got the static default wrong.
    """
    mapping = role_to_agent or _DEFAULT_ROLE_TO_AGENT
    return [r for r, a in mapping.items() if a == agent]


def _agent_role(agent: str, role_to_agent=None) -> str:
    """The single role ``agent`` holds, or ``""`` (#299).

    ⛔Ambiguity is reported as absence, not resolved by picking one. When a
      provider holds several roles there is no single answer, and naming one in
      an audit record would be a guess dressed as a fact. The worktree resolver
      makes the same call for the same reason.
    """
    roles = _roles_for_agent(agent, role_to_agent)
    return roles[0] if len(roles) == 1 else ""


def _agent_worktree(worktree_map, agent: str, role: str = "",
                    *, role_to_agent=None) -> str:
    """The worktree ``agent`` actually works in, or ``""`` (#292 review).

    ``worktree_map`` is keyed by role in role-based mode and by agent
    otherwise, so both spellings are handled — but only ever for THIS agent.

    ⛔Resolved through the LIVE ``role_to_agent`` mapping, not a static default.
      state.json supports custom assignments and the same provider on several
      roles, and the first version used the hardcoded
      `{claude: implementer, ...}` — so `(map, "claude", "reviewer")` returned
      the IMPLEMENTER worktree and the `role` argument did nothing. A configured
      Claude reviewer was therefore sized by the implementer's transcript and
      could be `/clear`ed on it (round-2 review of PR #293).

    ⛔Role identity is preserved when one provider holds several roles: the
      target role wins if the agent actually holds it. When it does not — an
      `agent_override` pointing at a provider whose roles are all different —
      a single role is unambiguous and used, but several is not, and the answer
      is ``""``. Guessing between two worktrees is how this whole class of bug
      arises; unknown reads downstream as "do not clear".

    ⛔Never falls back to the task's own role worktree either. With
      `agent_override` the pane belongs to one provider while
      `worktree_map[role]` belongs to another, and handing back the role's
      worktree would measure a directory the target agent does not own.
    """
    if not worktree_map or not agent:
        return ""
    direct = worktree_map.get(agent)
    if direct:
        return direct
    roles = _roles_for_agent(agent, role_to_agent)
    if role and role in roles and worktree_map.get(role):
        return worktree_map[role]
    if len(roles) == 1 and worktree_map.get(roles[0]):
        return worktree_map[roles[0]]
    return ""


def _context_token_count(pane_id: str, worktree_path: str = "",
                         *, agent: str = "", home=None) -> tuple:
    """``(tokens, source)`` for the context a pane is carrying (#292).

    ``tokens`` is ``None`` when nothing could be measured — never ``0``, which
    is a measured empty window.

    Source order, and why:

      * ``"transcript"`` — the pane's own session store, the same ground truth
        #284 reads. Exact, independent of whatever the terminal happens to be
        rendering, and it cannot be silently broken by a footer redesign;
      * ``"pane_hint"`` — the #133 screen scrape, kept only for providers with
        no transcript to read;
      * ``"unknown"`` — neither. Reported as such rather than as a number,
        because a caller thresholding on a fabricated 0 is exactly the failure
        #292 describes.
    """
    # ⛔Gated on the TARGET agent, not merely on having a path. The transcript
    #   reader is Claude-specific: pointing it at another provider's worktree
    #   reads whatever stale Claude session happens to sit there, and pointing
    #   it at Claude's worktree while the task is routed to another provider's
    #   pane sizes one pane by another's window. `agent_override` produces both
    #   (review of PR #293). If we cannot say the pane is Claude's, we cannot
    #   say the transcript is the one it is carrying.
    if agent == "claude" and worktree_path:
        tokens, _ = claude_context_tokens(worktree_path, home=home)
        if tokens is not None:
            return (tokens, "transcript")
    hinted = _pane_token_count(pane_id)
    if hinted is not None:
        return (hinted, "pane_hint")
    return (None, "unknown")


def _should_clear_context(tokens: Optional[int], *, threshold: int) -> bool:
    """Should the pane be cleared before the next push?

    ⛔``None`` does NOT clear. A forced reset on a reading nobody took throws
      away a working context, and the asymmetry runs the other way from the cap
      logic elsewhere: here the cost of acting on unknown is immediate and
      certain, while the cost of not acting is bounded by the next dispatch that
      CAN measure. The caller logs the unknown rather than letting it look like
      a small number, which is the part that was missing (#292).
    """
    return tokens is not None and tokens >= threshold


def _pane_clear_context(pane_id: str, *, task_id: str = "", project: str = "",
                        role: str = "", agent: str = "", worktree_path: str = "",
                        context_tokens=None, token_source: str = "",
                        threshold=None, events_path: str = "",
                        queue=None) -> bool:
    """Send /clear to a pane and RECORD that it happened (#133, #297).

    #293 made the 200k actuator reliable — five of seven worktrees were over
    threshold when it was measured — and this function emitted nothing at all.
    An unrecorded clear contaminates the resume-vs-fresh benchmark directly: the
    provider runs with cleared state while the economics stay attached to a
    context whose recorded policy still says `resume`.

    ⛔The outcome is ``attempted``, never ``completed``. `send-keys` returning 0
      proves the keystrokes were delivered to the pane, not that the provider
      acted on them; there is no confirmation channel here, and claiming one
      would invent a fact. A non-zero return IS informative and is recorded as
      ``send_failed``.

    Returns whether the keystrokes were sent.
    """
    r = subprocess.run(["tmux", "send-keys", "-t", pane_id, "/clear", "Enter"],
                       capture_output=True, text=True)
    sent = getattr(r, "returncode", 1) == 0
    if events_path:
        # Identity is READ, never minted: the push path has not resolved a
        # context yet, and asking `get_or_create_context` here would bump a
        # generation as a side effect of describing one. `task_id` is the join
        # key regardless; this is the audit detail (#297).
        identity = {}
        if queue is not None and project and agent and worktree_path:
            try:
                identity = queue.peek_context_identity(project, agent, worktree_path)
            except Exception:  # noqa: BLE001 — telemetry never breaks a push
                identity = {}
        try:
            record_context_event(
                events_path, "provider_context_cleared",
                task_id=task_id, project=project, role=role, agent=agent,
                provider=agent, pane_id=pane_id,
                context_id=identity.get("context_id"),
                context_generation=identity.get("context_generation"),
                provider_session_id=identity.get("provider_session_id"),
                context_tokens=context_tokens,
                token_source=token_source or None,
                cap_tokens=threshold,
                reason="auto_clear_token_threshold",
                outcome="attempted" if sent else "send_failed",
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                f"_pane_clear_context: could not record the clear of {pane_id}")
    import time as _time
    _time.sleep(2.0)
    return sent


def _mark_auto_cleared(queue, task, *, context_tokens=None,
                       token_source: str = "") -> None:
    """Record on the task that its pane was cleared right before the push (#297).

    ⛔`context_reset` is the correction, not a new concept. It is what an
      operator sets to force a fresh context, and after a real `/clear` it is
      simply true — so the next resolution bumps the generation and records
      `fresh`. A cohort keyed on the recorded policy then cannot pick this task
      up as an ordinary resume, which is the contamination #297 describes.

    ⛔The `auto_clear_*` fields are additive and separate so a consumer can
      EXCLUDE or stratify auto-cleared rows specifically, rather than seeing
      `fresh` and having to guess which of several reasons produced it.

    An operator's explicit `context_reset` is left alone: their intent must not
    be relabelled as an auto-clear.
    """
    try:
        existing = task.context if isinstance(task.context, dict) else {}
        extra = {
            "context_reset": True,
            "auto_cleared_before_push": True,
            "auto_clear_context_tokens": context_tokens,
            "auto_clear_token_source": token_source or None,
        }
        queue.patch_context(task.task_id, extra)
        task.context = {**existing, **extra}
    except Exception:  # noqa: BLE001 — never let bookkeeping block a push
        logger.exception(
            f"_mark_auto_cleared: could not mark {task.task_id} as auto-cleared")


_GEMINI_PERMISSION_RE = re.compile(r"Allow execution of .+\?", re.IGNORECASE)


def _pane_dismiss_permission_prompt(pane_id: str) -> bool:
    """If a gemini permission prompt is visible, auto-select 'Allow for this session'.

    Returns True if a prompt was found and dismissed (#134).
    """
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane_id],
            capture_output=True, text=True,
        )
    except Exception:
        return False
    if not _GEMINI_PERMISSION_RE.search(r.stdout):
        return False
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "2", "Enter"],
                   capture_output=True)
    logger.info(f"_pane_dismiss_permission_prompt: dismissed gemini prompt on {pane_id}")
    return True


def _pane_has_task(pane_id: str) -> bool:
    """Return True if pane shows pending input (task marker or collapsed paste).

    Two signals mean the buffer is still parked in the composer:
    - ``=== AGENT_CREW TASK ===`` — short pastes render inline, marker visible.
    - ``[Pasted text`` — Claude Code collapses long pastes; marker is hidden,
      but the placeholder reveals that input wasn't submitted.

    Once Enter is processed the composer clears and both signals disappear.
    """
    r = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", pane_id],
        capture_output=True, text=True,
    )
    out = r.stdout
    return ("=== AGENT_CREW TASK ===" in out) or ("[Pasted text" in out)


# Backoff schedule (seconds) for the per-attempt wait after Enter. The first
# attempt mirrors the original 0.3s behaviour; subsequent attempts widen so
# transient post-completion UI states (e.g. "Crunched for…", footer redraws,
# cache compaction) have time to settle before the next Enter is sent.
_PUSH_RETRY_DELAYS = (0.3, 0.5, 1.0, 2.0)


def _default_push(pane_id: str, text: str) -> None:
    """Send task via tmux bracketed paste, then retry Enter until submitted.

    Bracketed-paste mode delivers the entire blob atomically. After paste we
    wait for the TUI to finish consuming it, then send Enter. Some Claude UI
    states (post-task footer animations, cache writes between back-to-back
    tasks — issue #74) drop the first Enter without submitting; we re-send
    Enter with backoff up to len(_PUSH_RETRY_DELAYS) times.
    """
    logger.debug(f"_default_push called: pane_id={pane_id}")
    task_id = text.split("\n")[1].split(": ")[1] if "task_id:" in text else "unknown"
    logger.info(f"PUSH START: task_id={task_id}, pane_id={pane_id}")

    r1 = subprocess.run(
        ["tmux", "load-buffer", "-"],
        input=text,
        text=True,
        capture_output=True,
    )
    logger.debug(f"load-buffer result: rc={r1.returncode}, stderr={r1.stderr[:100] if r1.stderr else 'ok'}")

    r2 = subprocess.run(
        ["tmux", "paste-buffer", "-p", "-d", "-t", pane_id],
        capture_output=True,
    )
    logger.debug(f"paste-buffer result: rc={r2.returncode}, stderr={r2.stderr[:100] if r2.stderr else 'ok'}")

    # Give the TUI time to process the bracketed-paste sequence.
    time.sleep(0.5)

    for attempt, wait_after in enumerate(_PUSH_RETRY_DELAYS, start=1):
        subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"], capture_output=True)
        time.sleep(wait_after)
        if not _pane_has_task(pane_id):
            logger.info(
                f"PUSH SUCCESS: task_id={task_id} pushed to {pane_id} "
                f"(attempt {attempt}/{len(_PUSH_RETRY_DELAYS)})"
            )
            return
        logger.warning(
            f"PUSH retry: attempt {attempt}/{len(_PUSH_RETRY_DELAYS)} for "
            f"task_id={task_id} — composer still holding input"
        )

    logger.error(
        f"PUSH FAILED: task_id={task_id} still pending after "
        f"{len(_PUSH_RETRY_DELAYS)} Enter attempts on {pane_id}"
    )


def _format_reminder_message(task_id: str, port: int, idle_seconds: float, *, mcp_mode: bool = False) -> str:
    """Watchdog nudge: agent has been silent past the heartbeat threshold.

    In MCP mode (#162) emits a short one-liner — agents already have
    ``submit_result`` / ``bump_activity`` MCP tools, so the curl templates
    are legacy transport baggage that inflates model context unnecessarily.

    In push (legacy) mode, includes the full curl template so the agent can
    resolve the task in one paste even if the original block scrolled out of
    context.
    """
    if mcp_mode:
        return (
            f"AGENT_CREW REMINDER: task {task_id} idle {idle_seconds:.0f}s. "
            f"If still working call bump_activity(task_id='{task_id}'). "
            f"If done or blocked call submit_result(...)."
        )
    return (
        f"=== AGENT_CREW REMINDER ===\n"
        f"task_id: {task_id}\n"
        f"This pane has been silent for {idle_seconds:.0f}s with no sign of\n"
        f"activity. The crew stalls until you POST a result for this task.\n"
        f"\n"
        f"Pick one of the three paths below. Paste the curl block, edit the\n"
        f"placeholders, run it.\n"
        f"\n"
        f"1) FINISHED — POST status=\"completed\":\n"
        f"  curl -sS -X POST http://127.0.0.1:{port}/tasks/{task_id}/result \\\n"
        f"    -H 'Content-Type: application/json' \\\n"
        f"    -d '{{\"task_id\":\"{task_id}\",\"status\":\"completed\","
        f"\"summary\":\"...\",\"verdict\":null,\"findings\":[],\"pr_number\":null}}'\n"
        f"\n"
        f"2) STREAM/API TIMEOUT (partial response, can't recover) — POST\n"
        f"   status=\"failed\". The fallback policy will reroute this task\n"
        f"   to the next agent in the chain automatically:\n"
        f"  curl -sS -X POST http://127.0.0.1:{port}/tasks/{task_id}/result \\\n"
        f"    -H 'Content-Type: application/json' \\\n"
        f"    -d '{{\"task_id\":\"{task_id}\",\"status\":\"failed\","
        f"\"summary\":\"API stream timeout — partial response, no recovery\","
        f"\"verdict\":null,\"findings\":[],\"pr_number\":null}}'\n"
        f"\n"
        f"3) STILL WORKING — ignore this nudge. The next heartbeat will see\n"
        f"   the pane churning and reset the idle clock. If you can't tell\n"
        f"   why the pane went quiet, prefer path (2) over silence.\n"
        f"=== END REMINDER ===\n"
    )


# Per-task-type guard prefixes inserted at the top of the description we
# push to agents (Issue #110 phase 4-b). The task description is the
# message the agent's LLM reads first, so a clear directive here cuts
# off the "I'm a project developer, I'll just modify code" failure mode
# that hit alpha_engine #801–#805 even when the system prompt would
# have told the agent otherwise.
_TASK_TYPE_GUARDS: dict[str, str] = {
    "review": (
        "[REVIEW ONLY — do NOT modify or push code. Read the PR diff via "
        "`gh pr diff <pr_number>`, evaluate against the 3-layer checklist, "
        "and report verdict via `submit_result`.]"
    ),
    "test": (
        "[VERIFY ONLY — do NOT modify code, do NOT push, do NOT open or "
        "force-push a PR. Run the test suite in a clean checkout against "
        "the implementer's PR head and report pass/fail via `submit_result`.]"
    ),
}


def _guard_description(task: TaskRequest) -> str:
    """Prepend the task-type guard prefix to ``task.description`` if any.

    Implement and discuss tasks are returned unchanged. Review/test get
    a hard-coded prefix block — short, all-caps, in the language the
    agent's LLM is most likely to anchor on. Idempotent: if the
    description already starts with the guard, no double-prefix.
    """
    guard = _TASK_TYPE_GUARDS.get(task.task_type)
    if not guard:
        return task.description
    if task.description.startswith(guard):
        return task.description
    return f"{guard}\n\n{task.description}"


def _format_task_message(task: TaskRequest, port: int) -> str:
    ctx = json.dumps(task.context, ensure_ascii=False)
    description = _guard_description(task)
    return (
        f"=== AGENT_CREW TASK ===\n"
        f"task_id: {task.task_id}\n"
        f"task_type: {task.task_type}\n"
        f"branch: {task.branch}\n"
        f"priority: {task.priority}\n"
        f"context: {ctx}\n"
        f"description: {description}\n"
        f"=== END TASK ===\n"
        f"Do the work described above, then POST result: "
        f"curl -s -X POST http://127.0.0.1:{port}/tasks/{task.task_id}/result "
        f"-H 'Content-Type: application/json' "
        f"-d '{{\"task_id\":\"{task.task_id}\",\"status\":\"completed\",\"summary\":\"...\",\"findings\":[]}}'"
    )


def create_app(
    db_path: str,
    pane_map: Optional[dict] = None,
    port: int = 0,
    push_fn: Callable[[str, str], None] = _default_push,
    project: Optional[str] = None,
    pane_busy_fn: Callable[[str], bool] = _pane_is_busy,
    pane_liveness_fn=None,
    alive_timeout_multiplier: Optional[float] = None,
    watchdog_interval: Optional[float] = None,
    reminder_seconds: Optional[float] = None,
    timeout_seconds: Optional[float] = None,
    watchdog_disabled: Optional[bool] = None,
    anomaly_interval: Optional[float] = None,
    anomaly_disabled: Optional[bool] = None,
    state_path: Optional[str] = None,
    fallback_disabled: Optional[bool] = None,
    worktree_map: Optional[dict] = None,
    memory_provider: Optional[MemoryProvider] = None,
    shadow_memory_enabled: Optional[bool] = None,
    shadow_memory_timeout_seconds: Optional[float] = None,
) -> FastAPI:
    """
    pane_map: {role: pane_id} — e.g. {"implementer": "%475"}. If None, push is disabled.
    port: the HTTP port the server is listening on (embedded in task push messages so
    agents know where to POST results). Defaults to 0 (messages will say port 0).
    push_fn: injectable for testing.
    project: optional project name used to guard against cross-project review routing.
    pane_busy_fn: injectable pane-state probe for the watchdog. Defaults to
        ``_pane_is_busy`` (tmux capture-pane based).
    watchdog_interval: seconds between watchdog ticks. Falls back to env
        ``AGENT_CREW_WATCHDOG_INTERVAL`` then 30s.
    reminder_seconds: idle threshold (seconds) before pushing a reminder.
        Falls back to env ``AGENT_CREW_REMINDER_SECONDS`` then 300s.
    timeout_seconds: idle threshold (seconds) before auto-failing the task.
        Falls back to env ``AGENT_CREW_TIMEOUT_SECONDS`` then 900s.
    watchdog_disabled: skip the background loop entirely (tests). Falls back
        to env ``AGENT_CREW_WATCHDOG_DISABLED``.
    anomaly_interval: seconds between wrong-repo anomaly sweeps (Issue #80).
        Falls back to env ``AGENT_CREW_ANOMALY_INTERVAL`` then 600s.
    anomaly_disabled: skip the anomaly sweep entirely. Falls back to
        ``AGENT_CREW_ANOMALY_DISABLED`` (or auto-disabled when no
        ``AGENT_CREW_GH_USERNAME`` is configured).
    state_path: path to the per-project state.json — used by the anomaly
        sweep to auto-detect the expected repo allow-list.
    worktree_map: {role: worktree_path} — when provided the server prepares
        each worktree (fetch + branch checkout) before dispatching a task to
        it. Falls back to _load_worktree_map(state_path) if omitted.
    memory_provider: optional project-local historical-memory provider. Its
        retrieval is shadow telemetry only and can never alter dispatch.
    shadow_memory_enabled: explicit opt-in for shadow retrieval. Disabled by
        default, so even an injected provider receives zero calls until enabled.
    """
    logger.info("Context Pack effective enabled=%s (AGENT_CREW_CONTEXT_PACK=%r)",
                _cpack.enabled(), os.environ.get("AGENT_CREW_CONTEXT_PACK"))
    _memory_provider = memory_provider or NullMemoryProvider()
    if shadow_memory_enabled is None:
        shadow_memory_enabled = os.getenv("AGENT_CREW_SHADOW_MEMORY_ENABLED", "").lower() in (
            "1", "true", "yes",
        )
    if shadow_memory_timeout_seconds is None:
        try:
            shadow_memory_timeout_seconds = float(
                os.getenv("AGENT_CREW_SHADOW_MEMORY_TIMEOUT_SECONDS", "0.05"))
        except ValueError:
            shadow_memory_timeout_seconds = 0.05
    if worktree_map is None:
        worktree_map = _load_worktree_map(state_path) if not _WORKTREE_SYNC_DISABLED else {}
    if watchdog_interval is None:
        watchdog_interval = float(os.getenv("AGENT_CREW_WATCHDOG_INTERVAL", "30"))
    if reminder_seconds is None:
        reminder_seconds = float(os.getenv("AGENT_CREW_REMINDER_SECONDS", "300"))
    if timeout_seconds is None:
        timeout_seconds = float(os.getenv("AGENT_CREW_TIMEOUT_SECONDS", "900"))
    if pane_liveness_fn is None:
        pane_liveness_fn = _pane_liveness
    if alive_timeout_multiplier is None:
        # #231: how much longer a *demonstrably running* agent may stay quiet
        # before the watchdog reaps it. A leash, not an exemption — this
        # watchdog is the only bound on a pane-based task.
        alive_timeout_multiplier = float(
            os.getenv("AGENT_CREW_ALIVE_TIMEOUT_MULTIPLIER", "3")
        )
    if watchdog_disabled is None:
        watchdog_disabled = os.getenv("AGENT_CREW_WATCHDOG_DISABLED", "").lower() in (
            "1", "true", "yes",
        )
    if anomaly_interval is None:
        anomaly_interval = float(os.getenv("AGENT_CREW_ANOMALY_INTERVAL", "600"))
    if anomaly_disabled is None:
        anomaly_disabled = os.getenv("AGENT_CREW_ANOMALY_DISABLED", "").lower() in (
            "1", "true", "yes",
        )
    if fallback_disabled is None:
        fallback_disabled = os.getenv("AGENT_CREW_FALLBACK_DISABLED", "").lower() in (
            "1", "true", "yes",
        )

    # Phase 6a of the tmux→MCP cutover (Issue #119). The flag selects
    # whether the server actively pushes new tasks via tmux paste-buffer
    # (legacy ``push``), relies entirely on the agent's MCP pull loop
    # (``mcp``), or runs both paths concurrently (``both`` — default;
    # safe because MCP get_next_task is atomic and a task already
    # delivered via push transitions to in_progress before MCP could
    # return it).
    #
    # Anything unrecognized falls back to ``both`` so a config typo
    # never silently disables delivery.
    _delivery_raw = os.getenv("AGENT_CREW_DELIVERY", "both").strip().lower()
    if _delivery_raw not in ("push", "mcp", "both"):
        _delivery_raw = "both"
    _push_enabled = _delivery_raw in ("push", "both")

    _dispatcher_enabled = os.getenv("AGENT_CREW_DISPATCHER", "0").lower() not in ("0", "false", "no")

    state: dict = {}
    reminded_task_ids: set[str] = set()

    def _requeue_orphans() -> None:
        """On startup, reset in_progress tasks to pending and clean their worktrees.

        In dispatcher mode the server process owns agent subprocesses. A server
        restart means those subprocesses were killed, so any in_progress task is
        definitively incomplete and safe to re-queue.
        """
        tq = state["queue"]
        orphans = tq.list_tasks(status="in_progress")
        if not orphans:
            return
        logger.info(f"dispatcher: re-queuing {len(orphans)} orphaned in_progress task(s)")
        for task in orphans:
            role = task.context.get("role", "")
            wt = worktree_map.get(role) if worktree_map else None
            if wt and os.path.isdir(wt):
                try:
                    subprocess.run(
                        ["git", "checkout", "."],
                        cwd=wt, capture_output=True,
                    )
                    subprocess.run(
                        ["git", "clean", "-fd"],
                        cwd=wt, capture_output=True,
                    )
                    logger.info(f"dispatcher: cleaned worktree {wt} for task {task.task_id}")
                except Exception:
                    logger.exception(f"dispatcher: failed to clean worktree {wt}")
            tq.requeue(task.task_id)
            logger.info(f"dispatcher: re-queued task {task.task_id} (role={role})")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state["queue"] = TaskQueue(db_path)
        # #248: stamp the build into the durable event stream at startup, so a
        # production before/after cohort can be cut on the PROCESS boundary
        # instead of on a GitHub merge time. #247 showed those are not the same
        # boundary and that assuming they are attributes pre-fix behaviour to a
        # fix that never ran.
        try:
            _ident = _server_identity()
            _snap = _prov.snapshot(project=_ident["project"], port=_ident["port"])
            logger.info("build provenance: %s", _prov.summary_line(_snap))
            record_context_event(
                _context_events_path, "build_provenance",
                project=_ident["project"], db_path=_ident["db_path"],
                commit=_snap["commit"],
                ref=_snap["ref"], dirty=_snap["dirty"],
                code_fingerprint=_snap["code_fingerprint"],
                package_version=_snap["package_version"],
                started_at=_snap["started_at"], pid=_snap["pid"],
                source_root=_snap["source_root"], port=_ident["port"],
            )
        except Exception:
            logger.exception("build provenance record failed — continuing")
        background_tasks: list[asyncio.Task] = []
        if _dispatcher_enabled:
            _requeue_orphans()
            background_tasks.append(asyncio.create_task(_dispatcher_loop()))
        else:
            if not watchdog_disabled:
                background_tasks.append(asyncio.create_task(_watchdog_loop()))
            if not anomaly_disabled:
                background_tasks.append(asyncio.create_task(_anomaly_loop()))
        try:
            yield
        finally:
            for task in background_tasks:
                task.cancel()
            for task in background_tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    app = FastAPI(lifespan=lifespan)

    # #314 §3/§4: 라이브 cascade 도중 successor enqueue 시점에 STOP이 authoritative가 돼 enqueue가
    # PausedError로 원자 거부되면 — 부모 result는 이미 cascade_outbox에 result_json과 함께 원자
    # 저장돼 있으므로(§3), 부모 outbox를 'pending'으로 reopen한다. 재개 후 executor(replay)가 저장된
    # result로 전체 cascade를 멱등 재실행 → 거부됐던 successor까지 복구된다. (리뷰어가 지적한
    # 'PausedError가 result 없이 억제 기록 → replay 스킵'을, result-carrying outbox reopen으로 해결.)
    from fastapi.responses import JSONResponse as _JSONResponse
    from agent_crew.queue import PausedError as _PausedError

    @app.exception_handler(_PausedError)
    async def _paused_error_handler(request, exc):  # noqa: ANN001
        try:
            _m = re.search(r"/tasks/([^/]+)/result", str(request.url.path))
            _parent = _m.group(1) if _m else None
            if _parent:
                _reopened = state["queue"].outbox_reopen(_parent)
                logger.warning(f"[PAUSE-SUPPRESSED] enqueue race 차단: {exc} "
                               f"(parent={_parent}, outbox_reopened={_reopened}, path={request.url.path})")
            else:
                logger.warning(f"[PAUSE-SUPPRESSED] enqueue race 차단(파싱불가): {exc} (path={request.url.path})")
        except Exception:
            logger.exception("PausedError 핸들러 outbox reopen 실패(그래도 억제)")
        return _JSONResponse(status_code=200, content={
            "status": "ok", "suppressed_by_pause": True, "cascade_suppressed": True,
            "detail": "runtime STOP: execution-producing mutation atomically refused"})

    def q() -> TaskQueue:
        return state["queue"]

    def _guard_task_existence(task_id: str, target: str) -> bool:
        """Refuse a task block whose DB row is missing or terminal."""
        task_status = q().get_task_status(task_id)
        if task_status not in {"pending", "in_progress"}:
            reason = "task_missing" if task_status is None else f"task_status_{task_status}"
            logger.warning(
                "refusing tmux dispatch task_id=%s target=%s reason=%s",
                task_id, target, reason,
            )
            return False
        return True

    def _guard_tmx_push(task_id: str, target: str) -> str:
        """Return a live, project-owned pane id or refuse the dispatch."""
        if not _guard_task_existence(task_id, target):
            return ""
        pane_id = _resolve_tmux_pane_target(target)
        if not pane_id:
            logger.warning(
                "refusing tmux dispatch task_id=%s target=%s reason=pane_target_unresolvable",
                task_id, target,
            )
            _fail_if_active(task_id, "pane_target_unresolvable")
            return ""
        # Pane ownership changes on `crew recover` and pane-map reload.  Read
        # the durable state at the boundary, rather than freezing startup's
        # pane IDs and permanently refusing a legitimate replacement pane.
        owned_pane_ids, ownership_authoritative, ownership_reason = _recorded_pane_ids(
            state_path, pane_map,
        )
        if not ownership_authoritative:
            logger.warning(
                "tmux ownership fallback project=%s reason=%s target=%s resolved=%s",
                _server_identity()["project"] or "unknown",
                ownership_reason, target, pane_id,
            )
        if pane_id not in owned_pane_ids:
            logger.warning(
                "refusing tmux dispatch task_id=%s target=%s resolved=%s reason=pane_not_owned",
                task_id, target, pane_id,
            )
            if ownership_authoritative:
                _fail_if_active(task_id, "pane_not_owned")
            else:
                logger.warning("tmux ownership record unavailable; requeueing task_id=%s", task_id)
                q().requeue(task_id)
            return ""
        if not _pane_alive_for_push(pane_id):
            logger.warning(
                "refusing tmux dispatch task_id=%s target=%s resolved=%s reason=pane_dead",
                task_id, target, pane_id,
            )
            q().requeue(task_id)
            return ""
        return pane_id

    # Expose the exact push boundary for state-backed guard tests; production
    # dispatch still reaches it only through _try_push_next/_try_push_discuss.
    app.state.guard_tmx_push = _guard_tmx_push

    def _record_prepared_base(task: TaskRequest, role: str, prepared_sha: str,
                              caller: str) -> None:
        """Persist the exact prepared base, including an explicit unknown (#358)."""
        try:
            base_key = "worktree_base_sha" if role == "implementer" else "reviewed_sha"
            base_context = {
                base_key: prepared_sha or None,
                "worktree_base_status": "known" if prepared_sha else "unknown",
            }
            landed = (task.context or {}).get("sync_landed_bases", {})
            if isinstance(landed, dict):
                record = landed.get(role)
                if not isinstance(record, dict) and len(landed) == 1:
                    record = next(iter(landed.values()))
                if isinstance(record, dict):
                    base_context.update({
                        "sync_base_requested_ref": record.get("requested_ref"),
                        "sync_base_actual_ref": record.get("actual_ref"),
                        "sync_base_sha": record.get("sha"),
                        "sync_base_status": record.get("status", "unknown"),
                    })
            q().patch_context(task.task_id, base_context)
            task.context = {**(task.context or {}), **base_context}
        except Exception:
            logger.exception("%s: could not record prepared base for %s", caller, task.task_id)

    # Expose watchdog tick on app.state so tests can drive it deterministically
    # without the asyncio loop. Production code never reads this attribute.
    app.state.reminded_task_ids = reminded_task_ids

    def _panes_with_in_progress(exclude_task_id: str = "") -> set[str]:
        """지금 `in_progress` 인 task 들이 점유 중인 **pane 집합**.

        ⭐잠금 키를 `task_type` 에서 **실행 자원**으로 옮기기 위한 조회다
          (2026-09-23). 같은 task_type 이라는 이유로 빈 pane 이 막히던 결함을 없앤다.

        ⛔열거에 실패하면 **모든 pane 을 바쁜 것으로** 돌려준다(fail-closed).
          빈 집합을 돌려주면 "아무도 안 바쁘다" 로 읽혀 같은 pane 에 두 task 를
          밀어넣는다 — 이 함수가 막으려는 바로 그 사고다.
        """
        try:
            busy: set[str] = set()
            for _t in q().list_tasks(status="in_progress"):
                if exclude_task_id and _t.task_id == exclude_task_id:
                    continue
                _ctx = _t.context if isinstance(_t.context, dict) else {}
                _ov = (_ctx.get("agent_override") or "").strip().lower()
                _p = pane_map.get(_ov) if _ov else None
                if not _p:
                    _r = _TYPE_TO_ROLE.get(_t.task_type)
                    _p = pane_map.get(_r) if _r else None
                if _p:
                    busy.add(_p)
            return busy
        except Exception:
            logger.exception(
                "_panes_with_in_progress: could not enumerate in-progress tasks — "
                "treating every pane as busy (fail-closed)"
            )
            return set(pane_map.values())

    def _try_push_next(role: str) -> None:
        """If the role has an available pane and is idle, dequeue and push the next task."""
        # #314 §4 P0: replay 중에는 queue push 금지 — push는 다른 pending task를 claim/start하므로
        # replay가 ACK 전 죽으면 다음 replay가 또 다른 task를 claim/start(중복). replay는 successor
        # enqueue(durable transition)만; dispatch는 resume 후 정상 push 사이클/watchdog이 담당.
        if _REPLAYING.get():
            logger.debug(f"_try_push_next: replay 중 — push skip (role={role})")
            return
        logger.debug(f"_try_push_next: role={role}")
        if not _push_enabled:
            logger.warning(
                f"_try_push_next: AGENT_CREW_DELIVERY={_delivery_raw!r} — tmux push disabled. "
                "Tasks will only be delivered if an MCP client polls GET /tasks/next; "
                "if no MCP client is active they will accumulate until the watchdog auto-fails them."
            )
            return
        if not pane_map:
            logger.debug(f"_try_push_next: no pane_map")
            return
        pane_id = pane_map.get(role)
        if not pane_id:
            logger.debug(f"_try_push_next: role {role} not in pane_map")
            return
        task_type = _ROLE_TO_TYPE.get(role)
        if task_type is None:
            logger.warning(f"_try_push_next: role {role} not in _ROLE_TO_TYPE")
            return
        # ⛔예전에는 여기서 `q().has_in_progress(task_type)` 로 막았다 — **전역
        #   task_type 잠금**이라, 다른 pane 이 전부 비어 있어도 같은 task_type 이면
        #   두 번째 task 가 스케줄되지 않았다(2026-09-23 실측: pane 3개 중 실효 1개).
        #   잠금 키를 실행 자원(pane)으로 옮긴다 — 어느 pane 으로 갈지는 override 를
        #   해석해야 알 수 있으므로 **dequeue 뒤에** 판정하고, 충돌이면 되돌린다.
        task = q().dequeue(role=role)
        if task is None:
            logger.debug(f"_try_push_next: no pending task for role {role}")
            return  # nothing pending

        # Check if task has an agent_override in context
        _target_agent = _DISPATCH_ROLE_TO_AGENT.get(role, "")
        task_context = task.context if isinstance(task.context, dict) else {}
        logger.debug(f"_try_push_next: task_id={task.task_id}, context={task_context}")
        if "agent_override" in task_context:
            agent_override = task_context["agent_override"]
            override_pane_id = pane_map.get(agent_override)
            if override_pane_id:
                logger.info(f"_try_push_next: using agent override {agent_override} (pane {override_pane_id}) instead of role {role}")
                pane_id = override_pane_id
                # #292 review: the context measurement follows the pane, so it
                # has to follow the override too.
                _target_agent = agent_override
            else:
                logger.warning(f"_try_push_next: agent_override {agent_override} not found in pane_map")
                return

        # ⭐override 까지 해석한 **최종 pane** 이 이미 바쁘면 되돌린다.
        #   잠금 키는 task_type 이 아니라 실행 자원(pane)이다 — 같은 pane 에
        #   두 task 를 밀어넣지 않는다(test_u_sp02 가 이 줄을 지킨다).
        if pane_id in _panes_with_in_progress(exclude_task_id=task.task_id):
            logger.debug(
                f"_try_push_next: pane {pane_id} is busy — requeueing {task.task_id} "
                f"(role={role}, task_type={task.task_type})"
            )
            q().requeue(task.task_id)
            return

        # #140/#141: prepare worktree branch before task delivery.
        if worktree_map and not _WORKTREE_SYNC_DISABLED:
            wt_path = worktree_map.get(role)
            if wt_path:
                try:
                    _reviewed_sha = _prepare_worktree_for_task(
                        wt_path, task.task_id, task.branch or "", role,
                        task_context=task.context if isinstance(task.context, dict) else {},
                    )
                    # #253: persist it on the task BEFORE the push, so it also
                    # reaches the agent in the task block — a reviewer that can
                    # see which commit it was given can say so in its result,
                    # and a reviewer that cannot has no way to notice the head
                    # moved under it.
                    _record_prepared_base(task, role, _reviewed_sha, "_try_push_next")
                    logger.info(
                        f"_try_push_next: worktree prepared for {role} "
                        f"task_id={task.task_id} branch={task.branch or '(none)'}"
                    )
                except WorktreePrepRefused as exc:
                    # ⛔The broad handler below caught this and logged
                    #   "continuing with dispatch", so under
                    #   AGENT_CREW_DELIVERY=push/both a PR task whose head would
                    #   not resolve was still handed to an agent — #289's
                    #   fail-closed existed on the dispatcher path only (review
                    #   of PR #291). Both delivery paths prepare independently,
                    #   so both have to refuse.
                    #
                    # ⛔Marked, not just unpushed. The task is already claimed;
                    #   dropping it silently strands it `in_progress` until the
                    #   watchdog times it out, which reads as an agent that hung
                    #   rather than a target that could not be resolved.
                    logger.error(
                        f"_try_push_next: refusing to push {role} "
                        f"task_id={task.task_id} — {exc}"
                    )
                    _fail_if_active(task.task_id, getattr(exc, "reason", "worktree_prep_refused"),
                                    status="needs_human")
                    return
                except Exception:
                    logger.exception(
                        f"_try_push_next: worktree prep failed for {role} "
                        f"task_id={task.task_id} — continuing with dispatch"
                    )

        _push_worktree = worktree_map.get(role) if worktree_map else ""
        _push_project = task.project or os.path.basename(db_path.rstrip("/").rsplit("/", 2)[-2])
        if _push_worktree and not _ensure_role_protocol(
            role, _push_worktree, _push_project,
            os.path.join(os.path.dirname(db_path), "port"), agent=_target_agent, port=port,
        ):
            _fail_if_active(task.task_id, "missing_role_protocol")
            return

        # Guard every tmux side effect below (/clear, prompt dismissal, and the
        # task block itself) against a pane that is not ours.
        guarded_pane_id = _guard_tmx_push(task.task_id, pane_id)
        if not guarded_pane_id:
            return

        # #151: if target pane shows a usage-limit message, immediately reroute
        # via fallback rather than pushing into a blocked agent.
        if _pane_has_usage_limit(guarded_pane_id):
            blocked_agent = next(
                (k for k, v in (pane_map or {}).items() if v == pane_id and k in ("claude", "codex", "gemini")),
                None,
            )
            logger.warning(
                f"_try_push_next: pane {pane_id} shows usage-limit — "
                f"skipping push, routing task {task.task_id} to fallback "
                f"(blocked_agent={blocked_agent})"
            )
            usage_limit_summary = (
                f"usage limit detected on pane {pane_id}"
                + (f" (agent={blocked_agent})" if blocked_agent else "")
            )
            task_type_for_fb = _ROLE_TO_TYPE.get(role)
            if task_type_for_fb:
                fb_result = TaskResult(
                    task_id=task.task_id,
                    status="failed",
                    summary=usage_limit_summary,
                    verdict=None,
                    findings=[],
                    pr_number=None,
                )
                q().force_fail(task.task_id, usage_limit_summary)
                try:
                    _auto_fallback_failed_task(task.task_id, fb_result, task_type_for_fb)
                except Exception:
                    logger.exception(f"_try_push_next: fallback failed for usage-limited task {task.task_id}")
            else:
                q().requeue(task.task_id)
            return

        # #158: if pane shows a bare shell prompt (agent CLI crashed), requeue
        # the task instead of pushing bash commands into it.
        if _pane_has_bash_prompt(guarded_pane_id):
            logger.warning(
                f"_try_push_next: pane {pane_id} shows bare shell prompt — "
                f"agent CLI appears crashed. Requeuing task {task.task_id}."
            )
            q().requeue(task.task_id)
            return

        logger.info(f"_try_push_next: dequeued task_id={task.task_id}, calling push_fn")
        # #133: clear oversized context before pushing so claude doesn't stall.
        # #163: skip auto-clear in MCP mode — long-lived sessions benefit from
        # cache hits; clearing can turn cache-hit patterns into cache-create
        # spikes. Auto-clear only applies to push (tmux-paste) delivery.
        # #292: measure the pane's own transcript, not a footer that is usually
        # not on screen.
        #
        # ⛔Resolved independently of the prep block above rather than reusing
        #   its local: that block is skipped entirely when worktree sync is
        #   disabled, so borrowing its variable made this line unreachable-safe
        #   only by accident and raised UnboundLocalError on every push in that
        #   configuration. The measurement must not depend on whether prep ran.
        _tok_wt = _agent_worktree(worktree_map, _target_agent, role,
                                  role_to_agent=_DISPATCH_ROLE_TO_AGENT)
        tok, _tok_source = _context_token_count(guarded_pane_id, _tok_wt,
                                                agent=_target_agent)
        if _push_enabled and _should_clear_context(tok, threshold=_TOKEN_CLEAR_THRESHOLD):
            logger.info(
                f"_try_push_next: pane {pane_id} has {tok} tokens via {_tok_source} "
                f"(>= {_TOKEN_CLEAR_THRESHOLD}) — sending /clear before push"
            )
            _sent = _pane_clear_context(
                guarded_pane_id, task_id=task.task_id, project=task.project or "",
                role=role, agent=_target_agent, worktree_path=_tok_wt,
                context_tokens=tok, token_source=_tok_source,
                threshold=_TOKEN_CLEAR_THRESHOLD,
                events_path=_context_events_path, queue=q())
            if _sent:
                # #297: the provider is about to run with cleared state, so the
                # task must not be reported as an ordinary resume.
                _mark_auto_cleared(q(), task, context_tokens=tok,
                                   token_source=_tok_source)
            else:
                # ⛔Marking here would be worse than the bug #297 fixed: an
                #   UNcleared task recorded as `fresh` poisons that cohort
                #   instead of the resume one. The attempt is still recorded as
                #   `send_failed`, and this pane is now over threshold with
                #   nothing having cleared it (review of PR #299).
                logger.error(
                    f"_try_push_next: /clear could not be sent to {pane_id} — "
                    f"the pane is over threshold and was NOT cleared. Pushing "
                    f"anyway, unmarked; its context is unchanged (#297)."
                )
        elif _push_enabled and tok is None:
            # ⛔Visible, because this is the state that hid the bug for so long:
            #   it used to read as 0 and look like a healthy small context.
            logger.warning(
                f"_try_push_next: pane {pane_id} context size is UNKNOWN — no "
                f"transcript and no on-screen hint. Not clearing; this pane is "
                f"unguarded until something can measure it (#292)."
            )
        # #134: auto-dismiss gemini permission prompt if present.
        _pane_dismiss_permission_prompt(guarded_pane_id)
        # The row can change while preparing context; do not hand a terminal
        # or deleted task block to an otherwise healthy owned pane.
        if not _guard_task_existence(task.task_id, guarded_pane_id):
            return
        push_fn(guarded_pane_id, _format_task_message(task, port))
        # #152: record the moment the task was actually pushed to the pane
        # so the watchdog measures idle_for from push time, not dequeue time.
        q().set_push_at(task.task_id)

    #: How many times a push path found no pane to deliver to. Keyed by path so
    #: a persistent misconfiguration is loud once and then periodic (#260).
    _no_pane_warnings: dict = {}

    def _try_push_discuss(agent: Optional[str]) -> None:
        """Discuss tasks fan out per agent, not per role. pane_map is expected
        to hold agent-name keys (e.g. 'claude', 'codex', 'gemini') alongside
        the role keys. Busy-check and dequeue are both scoped to the agent so
        concurrent panelists don't block each other."""
        # #314 §4 P0: replay 중 push 금지(위 _try_push_next 참조).
        if _REPLAYING.get():
            logger.debug(f"_try_push_discuss: replay 중 — push skip (agent={agent})")
            return
        logger.debug(f"_try_push_discuss: agent={agent}")
        if not _push_enabled:
            logger.warning(
                f"_try_push_discuss: AGENT_CREW_DELIVERY={_delivery_raw!r} — tmux push disabled. "
                "Discuss tasks will only be delivered if an MCP client polls GET /tasks/next."
            )
            return
        if not pane_map or not agent:
            # #260: this was DEBUG, and it repeated forever while tasks kept
            # completing by other means — so a deployment with an empty
            # pane_map looked healthy from the logs. Still cheap: warn on the
            # first occurrence and then every 50th, so a persistent gap is
            # visible without flooding.
            _no_pane_warnings["discuss"] = _no_pane_warnings.get("discuss", 0) + 1
            n = _no_pane_warnings["discuss"]
            if n == 1 or n % 50 == 0:
                logger.warning(
                    f"_try_push_discuss: no pane_map or agent (agent={agent!r}, "
                    f"pane_map entries={len(pane_map or {})}) — discuss tasks "
                    f"cannot be pushed; occurrence {n}"
                )
            else:
                logger.debug("_try_push_discuss: no pane_map or agent")
            return
        pane_id = pane_map.get(agent)
        if not pane_id:
            logger.debug(f"_try_push_discuss: agent {agent} not in pane_map")
            return
        if q().has_discuss_in_progress_for_agent(agent):
            logger.debug(f"_try_push_discuss: discuss task in progress for agent {agent}")
            return
        task = q().dequeue_discuss_for_agent(agent)
        if task is None:
            logger.debug(f"_try_push_discuss: no pending discuss task for agent {agent}")
            return
        guarded_pane_id = _guard_tmx_push(task.task_id, pane_id)
        if not guarded_pane_id:
            return
        logger.info(f"_try_push_discuss: dequeued task_id={task.task_id} for agent={agent}, calling push_fn")
        # #260: the same oversized-context guard `_try_push_next` has had since
        # #133. `crew discuss` is the path these panels actually run on, and it
        # was the one without a check — so the panes that accumulated the most
        # context were exactly the ones nothing was watching.
        # #292: same transcript-first measurement. The discuss path knows the
        # agent rather than the role, so the worktree is resolved through the
        # role map — panels are exactly the panes that accumulate the most
        # context, which is why #260 added a guard here at all.
        _discuss_wt = _agent_worktree(worktree_map, agent,
                                      role_to_agent=_DISPATCH_ROLE_TO_AGENT)
        tok, _tok_source = _context_token_count(guarded_pane_id, _discuss_wt, agent=agent)
        if _push_enabled and _should_clear_context(tok, threshold=_TOKEN_CLEAR_THRESHOLD):
            logger.info(
                f"_try_push_discuss: pane {pane_id} has {tok} tokens via "
                f"{_tok_source} (>= {_TOKEN_CLEAR_THRESHOLD}) — sending /clear "
                f"before push"
            )
            _sent = _pane_clear_context(
                guarded_pane_id, task_id=task.task_id, project=task.project or "",
                role=_agent_role(agent, _DISPATCH_ROLE_TO_AGENT), agent=agent,
                worktree_path=_discuss_wt, context_tokens=tok,
                token_source=_tok_source, threshold=_TOKEN_CLEAR_THRESHOLD,
                events_path=_context_events_path, queue=q())
            if _sent:
                _mark_auto_cleared(q(), task, context_tokens=tok,
                                   token_source=_tok_source)
            else:
                logger.error(
                    f"_try_push_discuss: /clear could not be sent to {pane_id} "
                    f"— the pane is over threshold and was NOT cleared. Pushing "
                    f"anyway, unmarked (#297)."
                )
        elif _push_enabled and tok is None:
            logger.warning(
                f"_try_push_discuss: pane {pane_id} context size is UNKNOWN — no "
                f"transcript and no on-screen hint. Not clearing (#292)."
            )
        if not _guard_task_existence(task.task_id, guarded_pane_id):
            return
        push_fn(guarded_pane_id, _format_task_message(task, port))
        # #152: record push time for watchdog idle clock.
        q().set_push_at(task.task_id)

    def _resolve_pane_for_row(row: dict) -> Optional[str]:
        """Find the pane assigned to an in_progress task row. Mirrors the routing
        used by _try_push_next / _try_push_discuss so the watchdog inspects
        the same pane that received the task."""
        if not pane_map:
            return None
        ctx = row.get("context") or {}
        task_type = row["task_type"]
        if task_type == "discuss":
            agent = ctx.get("agent") if isinstance(ctx, dict) else None
            return pane_map.get(agent) if agent else None
        if isinstance(ctx, dict) and ctx.get("agent_override"):
            return pane_map.get(ctx["agent_override"])
        role = _TYPE_TO_ROLE.get(task_type)
        return pane_map.get(role) if role else None

    def _watchdog_tick(now: float) -> dict:
        """One pass of the heartbeat watchdog. Returns a summary of actions for
        observability and tests:

        - ``bumped``  — task_ids whose last_activity_at we refreshed
        - ``reminded`` — task_ids that received a nudge for the first time
        - ``timed_out`` — task_ids that we auto-failed
        """
        actions: dict = {"bumped": [], "reminded": [], "timed_out": []}
        if not pane_map:
            return actions

        rows = q().list_in_progress_with_activity()
        in_progress_ids = {r["task_id"] for r in rows}
        # Drop completed/failed tasks from the reminder dedupe set so a recycled
        # task_id (or a re-enqueued retry) doesn't get its reminder suppressed.
        reminded_task_ids.intersection_update(in_progress_ids)

        for row in rows:
            task_id = row["task_id"]
            pane_id = _resolve_pane_for_row(row)
            if not pane_id:
                continue
            # Every watchdog tmux interaction, including permission dismissal
            # and timeout Ctrl+C, must use the same live ownership boundary as
            # task delivery. Never inspect or interrupt a foreign pane.
            guarded_pane_id = _guard_tmx_push(task_id, pane_id)
            if not guarded_pane_id:
                continue
            pane_id = guarded_pane_id
            try:
                # #134: dismiss gemini permission prompt before busy-check so
                # the prompt doesn't freeze the pane and appear as idle.
                _pane_dismiss_permission_prompt(pane_id)
                if pane_busy_fn(pane_id):
                    q().bump_activity(task_id, ts=now)
                    actions["bumped"].append(task_id)
                    # Busy pane resets the reminder cycle — agent is alive.
                    reminded_task_ids.discard(task_id)
                    continue
            except Exception:
                logger.exception(f"watchdog: pane_busy_fn raised for {pane_id}")
                continue

            # #152: idle clock starts from push_at (when the task was actually
            # delivered to the pane) not from last_activity_at (which is set at
            # dequeue time, potentially while the pane was busy with a prior task).
            # Fall back to last_activity_at if push_at is 0 (MCP-dequeued tasks
            # that were never pushed) or if push_at is in the future (fake-time
            # tests where the push happened after the simulated now=).
            push_at = row.get("push_at") or 0.0
            last_act = row["last_activity_at"] or 0.0
            if push_at > 0 and push_at <= now:
                clock_start = max(push_at, last_act)
            else:
                clock_start = last_act if last_act > 0 else now
            idle_for = now - clock_start
            # #152: require at least one reminder before timing out. This
            # prevents a newly-dispatched task (whose pane was occupied by
            # a prior task) from being force-failed before it ever had a
            # chance to be picked up. A task that has never been reminded
            # has not yet had idle_for ≥ reminder_seconds confirmed, so we
            # treat it as still within its dispatch-grace window.
            # #231: a quiet pane is not necessarily a dead one — a full test
            # suite is silent for minutes. Ask what is actually running and
            # give a live process a longer leash before reaping it. Bounded,
            # not exempt: nothing else bounds a pane-based task.
            effective_timeout = timeout_seconds
            try:
                _liveness = pane_liveness_fn(pane_id)
            except Exception:
                logger.exception(f"watchdog: pane_liveness_fn raised for {pane_id}")
                _liveness = "unknown"
            if _liveness == "alive":
                effective_timeout = timeout_seconds * alive_timeout_multiplier
            if idle_for >= effective_timeout and task_id in reminded_task_ids:
                # Capture last 20 lines of pane for debugging (#167)
                pane_tail = ""
                try:
                    cap = subprocess.run(
                        ["tmux", "capture-pane", "-p", "-t", pane_id],
                        capture_output=True, text=True, timeout=3,
                    )
                    if cap.returncode == 0:
                        lines = [l for l in cap.stdout.splitlines() if l.strip()]
                        pane_tail = "\n".join(lines[-20:])
                except Exception:
                    pass
                summary = (
                    f"watchdog timeout: pane idle {idle_for:.0f}s without "
                    f"sign of activity (threshold {effective_timeout:.0f}s, "
                    f"agent process {_liveness})"
                )
                if pane_tail:
                    summary += f"\npane_tail:\n{pane_tail}"
                # #167: pass structured error_info so post-mortem queries have
                # machine-readable data, not just the free-form summary text.
                watchdog_error_info = {
                    "reason": "watchdog_timeout",
                    "idle_seconds": round(idle_for, 1),
                    "threshold_seconds": effective_timeout,
                    "agent_liveness": _liveness,
                    "pane_id": pane_id,
                }
                tt = q().force_fail(task_id, summary, error_info=watchdog_error_info)
                logger.error(
                    f"WATCHDOG TIMEOUT: task_id={task_id} marked failed; "
                    f"task_type={tt}, idle_for={idle_for:.0f}s"
                )
                reminded_task_ids.discard(task_id)
                actions["timed_out"].append(task_id)
                # Interrupt hung pane: kills child processes (e.g. gh pr view)
                # and breaks the LLM CLI out of infinite "Thinking". Safe on
                # idle panes — Ctrl+C on a shell prompt is a no-op.
                try:
                    subprocess.run(
                        ["tmux", "send-keys", "-t", pane_id, "C-c"],
                        capture_output=True, timeout=3
                    )
                except Exception:
                    logger.warning(f"watchdog: failed to interrupt pane {pane_id}")
                if tt is not None:
                    # Reuse the rate-limit fallback hook so a stuck pane gets
                    # routed to the next agent in the chain instead of just
                    # falling through to the same role's pending queue. The
                    # summary above contains "watchdog timeout" / "pane idle"
                    # patterns that `is_rate_limit_error` recognizes (#85).
                    synthetic_result = TaskResult(
                        task_id=task_id,
                        status="failed",
                        summary=summary,
                        verdict=None,
                        findings=[],
                        pr_number=None,
                    )
                    handled = False
                    try:
                        handled = _auto_fallback_failed_task(
                            task_id, synthetic_result, tt
                        )
                    except Exception:
                        logger.exception(
                            f"watchdog: fallback hook raised for {task_id}"
                        )
                    if not handled:
                        role = _TYPE_TO_ROLE.get(tt)
                        if role:
                            try:
                                _try_push_next(role)
                            except Exception:
                                logger.exception(
                                    f"watchdog: failed to push next task for role {role}"
                                )
            elif idle_for >= reminder_seconds and task_id not in reminded_task_ids:
                guarded_pane_id = _guard_tmx_push(task_id, pane_id)
                if not guarded_pane_id:
                    continue
                pane_id = guarded_pane_id
                # #173: if pane is stuck in bash error state (> prompt from
                # partial-text injection), send Ctrl+C to recover instead of
                # pushing a reminder that would be injected into bash again.
                if _pane_has_bash_prompt(pane_id):
                    logger.warning(
                        f"watchdog: pane {pane_id} in bash error/prompt state — "
                        f"sending Ctrl+C to recover instead of reminder for {task_id}"
                    )
                    try:
                        subprocess.run(
                            ["tmux", "send-keys", "-t", pane_id, "C-c"],
                            capture_output=True, timeout=3,
                        )
                    except Exception:
                        logger.warning(f"watchdog: failed to send Ctrl+C to {pane_id}")
                else:
                    try:
                        push_fn(pane_id, _format_reminder_message(task_id, port, idle_for, mcp_mode=not _push_enabled))
                    except Exception:
                        logger.exception(
                            f"watchdog: failed to push reminder for {task_id}"
                        )
                    else:
                        reminded_task_ids.add(task_id)
                    actions["reminded"].append(task_id)
                    logger.warning(
                        f"WATCHDOG REMINDER: task_id={task_id} idle for "
                        f"{idle_for:.0f}s — nudged pane {pane_id}"
                    )
        # #136: re-dispatch stale pending tasks that were never picked up.
        # A task stays "pending" when the target pane was busy at enqueue time
        # and no subsequent push fired. We nudge _try_push_next so the role's
        # pane gets another delivery attempt.
        stale_pending_seconds = float(
            os.getenv("AGENT_CREW_STALE_PENDING_SECONDS", "120")
        )
        try:
            stale = q().list_stale_pending(stale_pending_seconds, now)
        except Exception:
            logger.exception("watchdog: list_stale_pending raised — skipping")
            stale = []
        # #145: in MCP-only mode, tmux re-dispatch is a no-op. If tasks are still
        # pending after the stale window, no MCP client is connected — auto-fail them
        # so the crew doesn't hang silently forever.
        if not _push_enabled and stale:
            for sp in stale:
                tid = sp.get("task_id")
                if not tid:
                    continue
                summary = (
                    f"watchdog: AGENT_CREW_DELIVERY=mcp — no MCP client dequeued "
                    f"task {tid} within {stale_pending_seconds:.0f}s"
                )
                logger.error(f"watchdog #145 mcp-no-client auto-fail: {summary}")
                try:
                    q().force_fail_pending(tid, summary, error_info={
                        "reason": "mcp_no_client",
                        "stale_seconds": stale_pending_seconds,
                    })
                    actions.setdefault("mcp_no_client_failed", []).append(tid)
                except Exception:
                    logger.exception(f"watchdog #145: force_fail_pending raised for task {tid}")
            return actions
        # Collect unique roles so we only fire _try_push_next once per role.
        stale_roles: set[str] = set()
        for sp in stale:
            task_type = sp.get("task_type", "")
            ctx = sp.get("context") or {}
            if task_type == "discuss":
                agent = ctx.get("agent") if isinstance(ctx, dict) else None
                if agent and agent in (pane_map or {}):
                    stale_roles.add(f"discuss:{agent}")
            else:
                role = _TYPE_TO_ROLE.get(task_type)
                if role:
                    stale_roles.add(role)
        for role_key in stale_roles:
            try:
                if role_key.startswith("discuss:"):
                    _try_push_discuss(role_key.split(":", 1)[1])
                else:
                    _try_push_next(role_key)
                logger.info(f"watchdog: re-dispatched stale pending for role={role_key}")
            except Exception:
                logger.exception(
                    f"watchdog: re-dispatch failed for role={role_key}"
                )
        if stale_roles:
            actions["stale_redispatched"] = list(stale_roles)

        return actions

    # Stash the tick for tests; harmless in production (never read by handlers).
    app.state.watchdog_tick = _watchdog_tick

    async def _watchdog_loop() -> None:
        """Periodic background sweep. Cancels cleanly on shutdown."""
        try:
            while True:
                await asyncio.sleep(watchdog_interval)
                try:
                    _watchdog_tick(time.time())
                except Exception:
                    logger.exception("watchdog tick raised — continuing")
        except asyncio.CancelledError:
            return

    def _anomaly_tick() -> dict:
        """Sync entry point for the wrong-repo anomaly sweep (Issue #80)."""
        return check_wrong_repo(state_path=state_path)

    # Stash for tests (drive without the asyncio loop).
    app.state.anomaly_tick = _anomaly_tick

    async def _anomaly_loop() -> None:
        """Periodic wrong-repo sweep. Cancels cleanly on shutdown."""
        try:
            while True:
                await asyncio.sleep(anomaly_interval)
                try:
                    result = _anomaly_tick()
                    if result.get("anomalies"):
                        logger.warning(
                            f"anomaly sweep: {result['anomalies']} wrong-repo events "
                            f"(notified={result.get('notified')})"
                        )
                except Exception:
                    logger.exception("anomaly tick raised — continuing")
        except asyncio.CancelledError:
            return

    # ── Headless dispatcher (subprocess-per-task model) ──────────────────────
    # Built from state.json's "roles" field when present; falls back to the
    # hardcoded default (claude/codex/gemini). This is what lets the same
    # agent serve multiple roles (e.g. claude implementer + claude reviewer).
    _DISPATCH_ROLE_TO_AGENT: dict[str, str] = _load_role_to_agent(state_path)

    # task_id → transient retry count, used to decide whether an upstream
    # 429 (claude throttle / gemini capacity-exhausted) gets requeued or
    # finally failed. In-memory; resets on server restart.
    _transient_retries: dict[str, int] = {}
    try:
        _MAX_TRANSIENT_RETRY = int(os.getenv("AGENT_CREW_TRANSIENT_RETRY_MAX", "3"))
    except ValueError:
        _MAX_TRANSIENT_RETRY = 3

    # #202: append-only context lifecycle event stream, separate from
    # attribution.jsonl (see context_identity.record_context_event).
    _context_events_path = os.path.join(os.path.dirname(db_path), "context_events.jsonl")
    _attr_jsonl_path = os.path.join(os.path.dirname(db_path), "attribution.jsonl")
    # context_key → seen since this process started. A resume that's read
    # from a context_state row already present in the DB (i.e. NOT created
    # by this process) is a durable-restart recovery, not a first-touch
    # resume — the first such resolution per process gets its own
    # "context_recovered" event so restart-survival is directly observable
    # instead of just inferrable (#202 acceptance criterion).
    _seen_context_keys_this_process: set[str] = set()

    def _fail_if_active(task_id: str, reason: str, *, status: str = "failed") -> None:
        """End a task only when it is still in_progress (agent may have submitted first).

        `status` distinguishes two things the dispatcher used to conflate (#265):

          * `failed` — the work is known to have gone wrong (non-zero exit,
            exhausted quota, retries spent);
          * `timed_out` — we stopped waiting and do not know. The worker may
            still be running and may still POST a result, which is exactly what
            happened six times in one day on alpha_engine.

        Both are terminal for scheduling. Only the first is a failure, and a
        consumer deciding whether to re-issue work needs to tell them apart.
        """
        tasks = q().list_tasks(status="in_progress")
        if any(t.task_id == task_id for t in tasks):
            try:
                q().submit_result(
                    task_id,
                    TaskResult(task_id=task_id, status=status, summary=reason,
                               error_info={"reason": reason, "final": status == "failed"}),
                )
                _attr = q().get_attribution(task_id)
                record_context_event(
                    _context_events_path,
                    "task_failed" if status == "failed" else "task_timed_out",
                    task_id=task_id, reason=reason,
                    project=(_attr or {}).get("project"),
                    role=(_attr or {}).get("role"),
                    agent=(_attr or {}).get("agent"),
                    context_id=(_attr or {}).get("context_id"),
                )
                # #202 review finding 2: append the terminal state too, not
                # just the dispatch-time snapshot, so a JSONL-only consumer
                # can see this task actually failed.
                if _attr:
                    append_attribution_jsonl(_attr_jsonl_path, _attr)
            except Exception:
                logger.exception(f"_fail_if_active: could not fail task {task_id}")

    def _resolve_dispatch_target(task: TaskRequest, role: str) -> tuple[str, Optional[str]]:
        """Resolve the (agent, worktree_path) a task will actually dispatch
        into, honoring ``task.context["agent_override"]`` (#188) — e.g.
        `crew run --reviewer gemini`. Without this the CLI flag has no
        effect and review always routes to the role's default agent
        (codex), making it a SPOF.

        Pure/side-effect-free on purpose: shared by ``_dispatcher_loop``
        (to serialize dispatch by the *resolved* worktree, not just role —
        #202 review of PR #203, finding 1: two different roles' tasks can
        both resolve into the same overridden agent's worktree and, since
        role-level exclusivity doesn't see that, run concurrently against
        one provider `--continue` conversation and corrupt it) and
        ``_dispatch_task`` (to actually run it). Both callers resolving via
        the same function means they can never disagree about the target.
        """
        agent = _DISPATCH_ROLE_TO_AGENT.get(role, "claude")
        wt_override: Optional[str] = None
        _ctx = task.context if isinstance(task.context, dict) else {}
        _override = (_ctx.get("agent_override") or "").strip().lower() if isinstance(_ctx, dict) else ""
        if _override and _override != agent:
            for _r, _a in _DISPATCH_ROLE_TO_AGENT.items():
                if _a == _override:
                    _wt_candidate = worktree_map.get(_r)
                    if _wt_candidate:
                        wt_override = _wt_candidate
                        break
            if wt_override:
                agent = _override
        wt = wt_override or worktree_map.get(role)
        return agent, wt

    async def _dispatch_task(task: TaskRequest, role: str) -> None:
        """Spawn a headless agent subprocess for one task and await its exit."""
        _ctx = task.context if isinstance(task.context, dict) else {}
        _override = (_ctx.get("agent_override") or "").strip().lower() if isinstance(_ctx, dict) else ""
        agent, wt = _resolve_dispatch_target(task, role)
        if _override and _override != _DISPATCH_ROLE_TO_AGENT.get(role, "claude"):
            if agent == _override:
                logger.info(
                    f"dispatcher: agent_override {_override} → wt={wt} "
                    f"(task={task.task_id}, role={role})"
                )
            else:
                logger.warning(
                    f"dispatcher: agent_override={_override!r} has no worktree; "
                    f"falling back to role default agent={agent}"
                )
        logger.debug(f"dispatcher: _dispatch_task enter role={role!r} worktree_map_keys={list(worktree_map.keys())} task={task.task_id} agent={agent}")
        if not wt:
            logger.error(f"dispatcher: no worktree for role={role!r} worktree_map={worktree_map!r} task={task.task_id}")
            _fail_if_active(task.task_id, "no_worktree")
            return

        _project = task.project or os.path.basename(db_path.rstrip("/").rsplit("/", 2)[-2])

        # #272: a test stage runs alone in its worktree. The dispatcher's
        # `active_worktrees` set already refuses two concurrent tasks per
        # worktree, but it is one process's memory: it does not survive a
        # restart while a `start_new_session=True` child keeps running, and it
        # cannot see a second dispatcher at all. alpha_engine#5541 saw two
        # `make test` runs start 69s apart in one worktree. An flock outside
        # the process closes both gaps.
        #
        # ⛔Non-blocking, then requeue — the same shape as the worktree-collision
        #   branch in `_dispatcher_loop`. Parking a role slot on a blocking
        #   acquire would trade a concurrency bug for a stall.
        #
        # ⛔Taken HERE, ahead of the attribution row and `task_started`, not
        #   just before the subprocess. #204 pins `started_at` on first write
        #   and never overwrites it, so a deferred attempt used to stamp
        #   dispatch time and then hand the lock wait back as provider runtime
        #   — the exact conflation #278 exists to remove. A deferred attempt now
        #   writes no attribution, claims no context generation, and emits no
        #   `task_started`; it emits a deferral event instead.
        _lock_stack = contextlib.ExitStack()
        _lock_wait_seconds, _lock_defer_count = 0.0, 0
        if task.task_type == "test":
            if not _lock_stack.enter_context(test_stage_lock(wt)):
                _lock_stack.close()
                try:
                    _defers, _first_at = q().note_test_lock_defer(task.task_id)
                except Exception:
                    logger.exception(
                        f"dispatcher: could not count the lock deferral for {task.task_id}")
                    _defers, _first_at = 0, 0.0
                logger.info(
                    f"dispatcher: deferring test task={task.task_id} — another "
                    f"test stage already holds the lock for {wt} "
                    f"(defer #{_defers}, #272/#278)")
                try:
                    record_context_event(
                        _context_events_path, "test_stage_deferred",
                        task_id=task.task_id, project=_project, role=role, agent=agent,
                        task_type=task.task_type, defer_count=_defers,
                        waiting_since=_first_at,
                        lock_wait_seconds=round(max(0.0, time.time() - _first_at), 3)
                        if _first_at else 0.0,
                    )
                except Exception:
                    logger.exception(
                        f"dispatcher: deferral event failed for {task.task_id}")
                q().requeue(task.task_id)
                return
            # Acquired. Any earlier deferrals on this task are scheduler delay,
            # and this is the moment their total is finally knowable.
            try:
                _prior = q().get_task_context(task.task_id) or {}
                _lock_defer_count = _prior.get("test_lock_defer_count") or 0
                _first_at = _prior.get("test_lock_first_deferred_at") or 0.0
                if _lock_defer_count and _first_at:
                    _lock_wait_seconds = round(max(0.0, time.time() - float(_first_at)), 3)
            except Exception:
                logger.exception(
                    f"dispatcher: could not read lock-wait for {task.task_id}")
        # #202: capture the model in use where it's actually known. Only
        # gemini passes an explicit --model flag today; claude/codex rely on
        # their own CLI/config defaults with no reliable flag here, so their
        # model stays unknown (empty) rather than guessed. Resolved once,
        # here, and reused below when building the gemini `cmd` so the two
        # can't drift apart.
        _known_model = (
            os.getenv("AGENT_CREW_GEMINI_MODEL", "Gemini 3.7 Flash (Medium)")
            if agent == "gemini" else ""
        )

        # #202: resolve durable context identity before dispatch. A context
        # is scoped by (project, agent, worktree) — not role — since
        # agent_override can route a task from one role into another
        # agent's worktree and genuinely resume that agent's ongoing
        # conversation (Agent ≠ Role ≠ Context). An explicit
        # task.context["context_reset"] forces a new context/generation;
        # otherwise the very first dispatch into a (project, agent,
        # worktree) triple is automatically "fresh" and every later one
        # "resume"s it.
        _force_context_reset = bool(_ctx.get("context_reset")) if isinstance(_ctx, dict) else False
        # #236: an agy conversation resumed by `--continue` grows without
        # bound; alpha_engine's reached ~30k steps / 137 MB and every
        # dispatch re-sent it until quota 429'd. Trip a context reset at the
        # cap so the next dispatch starts a fresh provider conversation.
        # ⛔Nothing in agy's store is deleted or mutated — the oversized
        #   conversation is simply not resumed, so an in-flight context is
        #   never disturbed and the decision is reversible.
        # ⛔Initialised before the provider branches so the event gate below can
        #   read it for ANY agent, and named for the CONTEXT rather than for agy:
        #   it carries claude's cap decision too, and a name that says otherwise
        #   is how the event came to report every claude trip as `provider=agy`
        #   (#260 review). This boolean is the cap decision itself —
        #   `_force_context_reset` is not, because an operator's explicit
        #   task.context.context_reset sets it too (review-99ad8ad0).
        _ctx_over = False
        _ctx_cap_info = {}
        _codex_planned = ""      # the session a codex resume would use, if any
        if agent == "gemini":
            _ctx_over, _ctx_cap_info = agy_context_exceeds_cap(wt)
        elif agent == "codex":
            # #260 review: measurable now that #262 binds a resume to one
            # session. The rollout file is the conversation that resume
            # replays, so it is the same measurement as the other two providers.
            #
            # ⛔Resolve WHICH session first. The resume below prefers the
            #   durable `provider_session_id`, which can be an older rollout
            #   than the newest for this cwd — so measuring "newest" could let
            #   an oversized stored session resume uncapped, or reset a healthy
            #   one because an unrelated newer rollout was large. Peeked
            #   without minting a context, since the cap decision feeds the
            #   `force_reset` that minting depends on.
            _codex_planned = (q().peek_context_provider_session_id(
                _project, agent, wt) or "").strip()
            if not _codex_planned:
                _codex_planned = codex_session_for_cwd(wt)
            _ctx_over, _ctx_cap_info = codex_context_exceeds_cap(
                wt, session_id=_codex_planned)
        elif agent == "claude":
            # #260: the same defect on the other provider. `--continue` was
            # unconditional here, so the session never rotated — one file per
            # worktree since 2026-08-21, alpha_engine's at 290 MB. Sizing is
            # provider-specific; everything after this line is not.
            _ctx_over, _ctx_cap_info = claude_context_exceeds_cap(wt)
        if _ctx_over:
            _force_context_reset = True
            logger.warning(
                "dispatcher: %s context %s for %s is %.1f MB (cap %.0f MB) — "
                "forcing a fresh provider conversation (#236, #260)",
                _ctx_cap_info.get("provider", agent),
                _ctx_cap_info.get("conversation_id", "?"), wt,
                _ctx_cap_info.get("bytes", 0) / 1048576.0,
                _ctx_cap_info.get("cap_mb", 0),
            )
        _ctx_info = q().get_or_create_context(
            project=_project, agent=agent, worktree_path=wt, role=role,
            task_id=task.task_id, force_reset=_force_context_reset,
        )
        _context_key = _ctx_info["context_key"]
        _is_recovery = (
            _ctx_info["context_policy"] == "resume"
            and _context_key not in _seen_context_keys_this_process
        )
        _seen_context_keys_this_process.add(_context_key)
        if _ctx_info["context_policy"] == "fresh":
            _ctx_event_type = "context_created" if _ctx_info["context_generation"] == 1 else "context_reset"
        elif _is_recovery:
            # First time THIS process has resolved a context row it didn't
            # create itself — it must have survived a restart (#202
            # "context/task lifecycle survives Agent Crew restart").
            _ctx_event_type = "context_recovered"
        else:
            _ctx_event_type = "context_resumed"
        try:
            record_context_event(
                _context_events_path, _ctx_event_type,
                task_id=task.task_id, project=_project, role=role, agent=agent,
                context_id=_ctx_info["context_id"],
                context_generation=_ctx_info["context_generation"],
                session_task_index=_ctx_info["session_task_index"],
                previous_task_id=_ctx_info["previous_task_id"],
            )
            # A provider swap relative to the role's *configured default*
            # agent means retry/fallback routing redirected this dispatch —
            # surface it as its own event so that lineage doesn't have to
            # be re-derived from task.context on every read.
            # #236: the generic context_reset event does not say WHY. Emit
            # the measured cause so a reset forced by the size cap is
            # distinguishable from an operator-requested one.
            # Gate on the cap decision, never on "a reset happened and the
            # conversation has some bytes" — every conversation has bytes, and
            # an operator reset would then be mislabelled as a cap trip,
            # corrupting exactly the signal #236 added this event to measure.
            if _ctx_over:
                record_context_event(
                    _context_events_path, "provider_context_capped",
                    task_id=task.task_id, project=_project, role=role, agent=agent,
                    context_id=_ctx_info["context_id"],
                    context_generation=_ctx_info["context_generation"],
                    # #260 review: the provider that actually tripped the cap.
                    # Hardcoding "agy" here predated claude having a cap at all,
                    # and once it did, every claude trip was telemetered as agy —
                    # corrupting the one field that says which store overflowed.
                    provider=_ctx_cap_info.get("provider", agent),
                    conversation_id=_ctx_cap_info.get("conversation_id", ""),
                    bytes=_ctx_cap_info.get("bytes", 0),
                    cap_mb=_ctx_cap_info.get("cap_mb", 0),
                    # #284: bytes alone cannot say WHY a claude session tripped,
                    # and on this fleet the two signals barely correlate — a
                    # 9.33 MB store carried a larger window than a 32.53 MB one.
                    # `tripped_by` makes a reset attributable to a cause; the
                    # window is reported whether or not a token cap is set.
                    context_tokens=_ctx_cap_info.get("context_tokens"),
                    cap_tokens=_ctx_cap_info.get("cap_tokens", 0),
                    tripped_by=_ctx_cap_info.get("tripped_by", ""),
                )
            elif _ctx_cap_info:
                # #288: the normal-traffic row. #285 computed the window on
                # every dispatch and then dropped it — the only durable sink was
                # the cap event above, which fires under `if _ctx_over:`, and
                # with the token cap off by default (#284) that is never. So the
                # fleet's measured 500k–600k windows produced no rows at all and
                # the threshold decision #284 deferred to the quota layer had
                # nothing to stand on.
                #
                # ⛔A DISTINCT event. `provider_context_capped` means "a reset
                #   was forced"; overloading it to also mean "here is a number"
                #   would corrupt the one signal that already works. And exactly
                #   one row per dispatch — this is the `elif` of the cap, so an
                #   observation and a cap can never both be counted.
                #
                # ⛔Guarded on `_ctx_cap_info` being non-empty, which means a
                #   measurement was ATTEMPTED. "Not measured" and "measured,
                #   unknown" are different: the first leaves no row, the second
                #   leaves one with `context_tokens: null`. Dropping unknowns
                #   instead would bias the sample toward readable sessions.
                record_context_event(
                    _context_events_path, "provider_context_observed",
                    task_id=task.task_id, project=_project, role=role, agent=agent,
                    task_type=task.task_type,
                    context_id=_ctx_info["context_id"],
                    context_generation=_ctx_info["context_generation"],
                    provider=_ctx_cap_info.get("provider", agent),
                    provider_session_id=_ctx_cap_info.get("conversation_id", ""),
                    # `.get(key)` without a default on purpose: absent stays
                    # None, so unknown is null and a measured zero is 0.
                    context_tokens=_ctx_cap_info.get("context_tokens"),
                    context_bytes=_ctx_cap_info.get("bytes"),
                    cap_mb=_ctx_cap_info.get("cap_mb", 0),
                    cap_tokens=_ctx_cap_info.get("cap_tokens", 0),
                )
            _role_default_agent = _DISPATCH_ROLE_TO_AGENT.get(role)
            if _role_default_agent and agent != _role_default_agent:
                record_context_event(
                    _context_events_path, "provider_fallback",
                    task_id=task.task_id, project=_project, role=role,
                    from_agent=_role_default_agent, to_agent=agent,
                    context_id=_ctx_info["context_id"],
                )
            record_context_event(
                _context_events_path, "task_started",
                task_id=task.task_id, project=_project, role=role, agent=agent,
                task_type=task.task_type, context_id=_ctx_info["context_id"],
            )
        except Exception:
            logger.exception(f"dispatcher: context event emission failed for task={task.task_id}")

        # retry_of / fallback_of: lineage set by _auto_retry_failed_task and
        # pipeline.auto_fallback_failed_task respectively when they enqueue
        # a follow-up task (#202 — reconstructable without re-deriving from
        # each task's own context dict).
        _retry_of = _ctx.get("original_task_id", "") if "retry_attempt" in _ctx else ""
        _fallback_of = _ctx.get("fallback_from_task_id", "") if isinstance(_ctx, dict) else ""

        # Prepare worktree: stash local changes, fetch origin, checkout right branch.
        #
        # ⛔Moved AHEAD of the attribution/economics block by #289. Preparing
        #   after it meant a task whose target could not be resolved had already
        #   written an attribution row, a `test_scope_resolved` event and its
        #   #278 treatment fields — economics describing a test run that was
        #   about to not happen. Refusing early leaves no row to correct.
        if not _WORKTREE_SYNC_DISABLED:
            try:
                _reviewed_sha = _prepare_worktree_for_task(
                    wt, task.task_id, task.branch or "", role,
                    task_context=task.context if isinstance(task.context, dict) else {},
                )
                # #253/#358: record the exact prepared commit, or explicit
                # unknown, before prompt construction.  A prep failure still
                # dispatches, but can no longer masquerade as an unrecorded
                # stale base later in the task lineage.
                _record_prepared_base(task, role, _reviewed_sha, "dispatcher")
                logger.info(
                    f"dispatcher: worktree prepared for {role} "
                    f"task_id={task.task_id} branch={task.branch or '(none)'} "
                    f"at {(_reviewed_sha or '?')[:9]}"
                )
            except WorktreePrepRefused as exc:
                # ⛔needs_human, not failed and not a retry. A retry would make
                #   the same `gh` call again; a `failed` routes into the
                #   fallback/retry machinery and eventually spends another
                #   provider on the same unanswerable question. Someone has to
                #   look at why the PR head cannot be resolved (#289).
                logger.error(
                    f"dispatcher: refusing to dispatch {role} task_id={task.task_id} "
                    f"— {exc}"
                )
                _lock_stack.close()
                _fail_if_active(task.task_id, getattr(exc, "reason", "worktree_prep_refused"),
                                status="needs_human")
                return
            except Exception:
                logger.exception(
                    f"dispatcher: worktree prep failed for {role} task_id={task.task_id} — continuing"
                )

        if not _ensure_role_protocol(
            role, wt, _project, os.path.join(os.path.dirname(db_path), "port"), agent=agent, port=port,
        ):
            _lock_stack.close()
            _fail_if_active(task.task_id, "missing_role_protocol")
            return

        # Record durable attribution before dispatch so quota systems can map
        # token usage back to the project even after worktrees are torn down (#174).
        try:
            _repo_url = subprocess.run(
                ["git", "-C", wt, "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            _git_branch = subprocess.run(
                ["git", "-C", wt, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if _git_branch == "HEAD":
                # #280: a detached worktree reports the literal string "HEAD",
                # which is not a branch and joins to nothing downstream. Record
                # what the task was dispatched FOR — the fix that stopped us
                # owning the ref must not also erase which branch this is about.
                _git_branch = (task.branch or "").strip() or "HEAD"
            # #262: bind codex to a session that belongs to THIS worktree.
            # `resume --last` is global, so the binding has to be ours: prefer
            # the provider_session_id already recorded for this context, then a
            # bounded search of codex's own store, then nothing — and "nothing"
            # means fresh, never a guess.
            _codex_session = ""
            if agent == "codex" and _ctx_info["context_policy"] == "resume":
                # The session the cap already measured — resolved the same way,
                # once, so the two decisions cannot disagree.
                _codex_session = (_ctx_info.get("provider_session_id")
                                  or _codex_planned or "").strip()
                if not _codex_session:
                    _codex_session = codex_session_for_cwd(wt)
                    if _codex_session:
                        logger.info(
                            "dispatcher: bound codex session %s to %s from its own "
                            "rollout store (#262)", _codex_session[:8], wt)
                if not _codex_session:
                    logger.info(
                        "dispatcher: no codex session recorded for %s — starting a "
                        "fresh one rather than resuming whatever ran last on this "
                        "host (#262)", wt)

            q().record_attribution(
                task_id=task.task_id,
                project=_project,
                agent=agent,
                role=role,
                task_type=task.task_type,
                worktree_path=wt,
                repo_url=_repo_url,
                git_branch=_git_branch,
                status="in_progress",
                model=_known_model,
                context_id=_ctx_info["context_id"],
                provider_session_id=(_codex_session
                                     or _ctx_info.get("provider_session_id") or ""),
                context_policy=_ctx_info["context_policy"],
                context_generation=_ctx_info["context_generation"],
                session_task_index=_ctx_info["session_task_index"],
                previous_task_id=_ctx_info.get("previous_task_id") or "",
                retry_of=_retry_of,
                fallback_of=_fallback_of,
            )
            if agent == "claude":
                # Claude's append-only transcript is shared by sequential
                # tasks in this worktree. Persist its pre-invocation byte
                # offset so result handling reads only this task's suffix.
                q().patch_context(task.task_id, {"claude_transcript_start":
                    claude_task_start_boundary(
                        wt, provider_session_id=_ctx_info.get("provider_session_id") or "")})
            # #278: the tester treatment, as a structured field rather than a
            # sentence in the agent's summary. Resolved on the dispatch path so
            # it reflects the config in force for THIS task — an operator can
            # change the scope between two tasks of one run, and a cohort built
            # from setup-time state would silently mix the two.
            if task.task_type == "test":
                _scope = _load_test_scope(wt, _project)
                # Council #39 Tier 1 has a fixed verification budget: changed
                # scope plus the usual cross-cutting guards, never an
                # operator-configured full-suite override. The cascade stores
                # this decision on the task so replay/restart cannot infer it
                # from a provider or project name.
                if isinstance(task.context, dict) and task.context.get("test_scope") == "targeted":
                    _scope = {**_scope, "full_suite": False,
                              "source": "risk_tier", "source_kind": "risk_tier"}
                _scope_name = _effective_scope(_scope)
                _scope_hash = _scope_fingerprint(_scope)
                q().record_test_economics(
                    task.task_id,
                    effective_test_scope=_scope_name,
                    test_scope_source=_scope.get("source_kind", "builtin"),
                    test_scope_hash=_scope_hash,
                    lock_wait_seconds=_lock_wait_seconds,
                    lock_defer_count=_lock_defer_count,
                )
                record_context_event(
                    _context_events_path, "test_scope_resolved",
                    task_id=task.task_id, project=_project, role=role, agent=agent,
                    context_id=_ctx_info["context_id"],
                    effective_test_scope=_scope_name,
                    # ⛔The categorical kind, never `scope["source"]` — that is
                    #   a filesystem path, and this stream is published to the
                    #   quota systems.
                    test_scope_source=_scope.get("source_kind", "builtin"),
                    test_scope_hash=_scope_hash,
                    lock_wait_seconds=_lock_wait_seconds,
                    lock_defer_count=_lock_defer_count,
                )

            # Append-only JSONL for external quota scanners that outlive the
            # DB. Written from the DB row itself (not a hand-built dict) so
            # the two representations can't drift apart (#202 review of PR
            # #203, finding 2) — a second line gets appended at task
            # completion (see _fail_if_active and the /result endpoint)
            # with the same task_id and the terminal status/outcome/
            # completed_at, so a tail-only consumer can observe the final
            # result and not just this in-flight snapshot.
            _attr_row = q().get_attribution(task.task_id)
            if _attr_row:
                append_attribution_jsonl(_attr_jsonl_path, _attr_row)
        except Exception:
            logger.exception(f"dispatcher: attribution record failed for task={task.task_id}")


        message = _format_task_message(task, port)
        # #239: assemble a bounded, provenance-linked Context Pack from durable
        # project sources and prepend it. Opt-in (AGENT_CREW_CONTEXT_PACK) and
        # fail-soft: a retrieval failure yields a pack that SAYS it is degraded
        # rather than a silent empty one, so absence of an artifact is never
        # mistaken for absence of fact.
        _pack = None
        _state_dir = os.path.dirname(db_path)
        if _cpack.enabled():
            _pack = _cpack.build_pack_for_task(
                _ctx if isinstance(_ctx, dict) else {},
                task_id=task.task_id, task_type=task.task_type, role=role,
                repo_path=wt, branch=task.branch,
                episodes_path=os.path.join(_state_dir, "episodes.jsonl"),
                # #240: persisted procedures reach the dispatch from here.
                # Passed explicitly rather than derived inside the builder so
                # the wiring is visible at the call site — the previous
                # version's absence is exactly what review-2016dcf3 caught.
                procedures_path=os.path.join(_state_dir, "procedures.jsonl"),
                shadow_path=os.path.join(_state_dir, "procedure_shadow.jsonl"),
            )
            _block = _pack.to_prompt_block()
            if _block:
                message = _block + "\n\n" + message
            try:
                # ⛔Merged, not splatted alongside explicit kwargs. `telemetry()`
                #   already carries `role`, so passing `role=role` here raised
                #   `TypeError: got multiple values for keyword argument 'role'`
                #   on EVERY dispatch — swallowed by the except below, so the
                #   feature recorded nothing at all and said nothing about it
                #   (#258, found by quota-core while building the consumer).
                #   Building one dict makes the collision impossible rather than
                #   merely absent: the next key added to telemetry() cannot
                #   silently switch this off again.
                # ⛔Identity is applied LAST and therefore wins. Merging the
                #   other way round fixed the crash but handed the pack the
                #   power to relabel the event: a future telemetry key called
                #   `task_id` or `context_id` would silently attribute this
                #   pack to a different task, and an attribution record that
                #   lies is worse than one that is missing (#258 review).
                #   `role` is the one known overlap and carries the same value
                #   from both sides.
                _identity = {
                    "task_id": task.task_id,
                    "project": _project,
                    "role": role,
                    "agent": agent,
                    "context_id": _ctx_info["context_id"],
                    "context_generation": _ctx_info["context_generation"],
                }
                _telemetry = _pack.telemetry()
                _shadowed = (set(_telemetry) & set(_identity)) - {"role"}
                if _shadowed:
                    # Dropping a telemetry field silently is a smaller harm than
                    # mislabelling the event, but it is still a harm — say it,
                    # so the collision is fixed rather than absorbed.
                    logger.warning(
                        "dispatcher: context pack telemetry carries dispatch "
                        "identity keys %s for task=%s; the dispatcher's values "
                        "win and the pack's are dropped (#258)",
                        sorted(_shadowed), task.task_id,
                    )
                _event_fields = {**_telemetry, **_identity}
                record_context_event(
                    _context_events_path, "context_pack_built", **_event_fields,
                )
                # Durable linkage: the pack that produced this dispatch is
                # recorded on the task, so a terminal outcome can be attributed
                # back to the exact context it was given.
                q().patch_context(task.task_id, {
                    "context_pack_id": _pack.pack_id,
                    "context_pack_hash": _pack.pack_hash,
                    "context_pack_degraded": _pack.degraded,
                })
                # #342(A) producer: degraded means an observed retrieval
                # failure/incomplete required context (false); a healthy pack
                # proves recall only when it contains items (true).  A disabled
                # search or empty healthy pack has no such observation, so it
                # remains NULL (unknown), never a guessed false or true.
                # This is telemetry only: any persistence failure must never
                # alter dispatch, admission, STOP, or the rendered message.
                try:
                    q().record_required_context_recalled(
                        task.task_id, _required_context_recalled_observation(_pack))
                except Exception:
                    logger.exception(
                        "dispatcher: required_context_recalled telemetry failed for %s",
                        task.task_id,
                    )
            except Exception:
                logger.exception(
                    f"dispatcher: context pack telemetry failed for {task.task_id}")

        # #322: Shadow-only optional durable-memory observation.  This sits
        # after the baseline message (including any Context Pack) is complete,
        # and its result is deliberately never assigned to `message`, `task`,
        # routing, retry, or context-policy state.  A provider failure is
        # converted to telemetry by `shadow_retrieve`, not an execution error.
        if shadow_memory_enabled:
            try:
                _shadow_result = shadow_retrieve_bounded(
                    _memory_provider, MemoryRequest(
                        project=_project,
                        task_id=task.task_id,
                        context_id=_ctx_info["context_id"],
                        agent_identity=agent,
                        context_generation=_ctx_info["context_generation"],
                        authoritative_ref=(
                            _ctx.get("reviewed_sha", "") if isinstance(_ctx, dict) else ""),
                        branch=task.branch,
                        commit_ref=(
                            _ctx.get("reviewed_sha", "") if isinstance(_ctx, dict) else ""),
                        memory_types=(
                            "procedural", "episodic", "decision", "failure_pattern", "evidence"),
                    ), shadow_memory_timeout_seconds,
                )
                _shadow_event = {
                    **shadow_telemetry(_shadow_result),
                    "task_id": task.task_id,
                    "project": _project,
                    "role": role,
                    "agent": agent,
                    "context_id": _ctx_info["context_id"],
                    "context_generation": _ctx_info["context_generation"],
                }
                record_context_event(
                    _context_events_path, "shadow_memory_retrieval", **_shadow_event,
                )
                # Durable telemetry only. The task object used to render `message`
                # remains untouched, so this write cannot influence this dispatch.
                q().patch_context(task.task_id, {"shadow_memory": {
                    key: value for key, value in _shadow_event.items()
                    if key not in {"task_id", "project", "role", "agent", "context_id", "context_generation"}
                }})
            except Exception:
                # Telemetry persistence is also non-critical; do not let it turn a
                # successful baseline dispatch into a memory-dependent failure.
                logger.exception("dispatcher: shadow memory telemetry failed for task=%s", task.task_id)
        # Per-role log file so `tail -f dispatch_{role}.log` in the pane
        # shows a continuous stream across all tasks for that role.
        log_path = os.path.join(os.path.dirname(db_path), f"dispatch_{role}.log")

        if agent == "claude":
            # #260: resume only when Agent Crew's own context policy says so —
            # the treatment #236 gave gemini and never gave claude. While
            # `--continue` was unconditional, a freshly minted context
            # (generation 1, an operator reset, or a cap trip) still resumed
            # the provider's old session, so identity and provider state
            # disagreed and the session could never rotate.
            cmd = ["claude", "-p", message]
            if _ctx_info["context_policy"] == "resume":
                cmd.append("--continue")
            cmd += ["--dangerously-skip-permissions",
                    "--verbose", "--output-format", "stream-json"]
        elif agent == "gemini":
            # gemini-cli + oauth-personal stopped serving on 2026-06-18
            # (IneligibleTierError) and the replacement, Antigravity CLI
            # (`agy`), now ships a CPU-compat build (1.0.10+) that runs on
            # this host. Switch the dispatched binary accordingly:
            #   `gemini --resume latest --yolo`  →  `agy --continue --dangerously-skip-permissions`
            #   `--output-format stream-json` has no equivalent — agy prints
            #   plain stdout, which the dispatcher captures into the log file
            #   the same way.
            # Pin model explicitly so kickoffs don't get routed to a rotating
            # default. agy 1.1.x switched --model to take the display name
            # from `agy models` rather than a slug (e.g. "gemini-3.5-flash"
            # now 400s with "model ... is not recognized"). Override via
            # AGENT_CREW_GEMINI_MODEL. (_known_model resolved earlier,
            # before dispatch, so the attribution record and this cmd can't
            # drift apart — #202.)
            # #236: resume only when Agent Crew's own context policy says
            # "resume". Previously `--continue` was unconditional, so a
            # freshly-minted context (generation 1, or an explicit
            # context_reset) still resumed the provider's old conversation —
            # identity and provider state could disagree. Now they cannot.
            cmd = ["agy", "-p", message]
            if _ctx_info["context_policy"] == "resume":
                cmd.append("--continue")
            cmd += ["--dangerously-skip-permissions", "--model", _known_model,
                    "--output-format", "json"]
        else:  # codex — resume last session for context continuity; falls back to fresh if none exists
            # #260: policy-aware for the same reason as claude above. Codex has
            # no per-worktree store to size — `~/.codex/sessions` is partitioned
            # by date, not by cwd, and `resume --last` means the last session
            # globally — so there is no cap to apply here, only the reset that
            # an explicit context_reset or a first-generation context implies.
            # Sizing codex would need a signal the CLI does not currently expose;
            # not inventing one.
            # #262: resume the session BOUND TO THIS WORKTREE, by id.
            # ⛔Never `--last`. It selects the newest session anywhere on the
            #   host, so a resume here could attach another project's provider
            #   state while telemetry still reported this project's logical
            #   context — an identity and economics mismatch, and a
            #   cross-project content leak.
            if _codex_session:
                cmd = ["codex", "exec", "resume", _codex_session,
                       "--dangerously-bypass-approvals-and-sandbox", "--json", message]
            else:
                cmd = ["codex", "exec",
                       "--dangerously-bypass-approvals-and-sandbox", "--json", message]

        timeout_secs = _dispatch_timeout_for_role(role)
        logger.info(f"dispatcher: {agent} task={task.task_id} role={role} wt={wt} timeout={timeout_secs}s")
        # Only pop the retry counter on a terminal outcome. Flipped to False
        # right before the early `return` on a successful requeue — that
        # `return` still runs `finally`, so without this flag the counter
        # was erased every attempt and _MAX_TRANSIENT_RETRY never actually
        # capped anything (#201).
        _terminal = True
        _task_log_tail = ""
        try:
            import datetime as _dt
            with open(log_path, "a") as log_f:
                log_f.write(
                    f"\n{'='*60}\n"
                    f"TASK {task.task_id} | {role} | "
                    f"{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"{'='*60}\n"
                )
            # Captured after the marker so the transient-error scan below
            # never reads into a prior task's leftover output (#200).
            _task_log_start_offset = os.path.getsize(log_path)
            # #236: window for correlating agy's own log with this task.
            _task_started_wall = time.time()
            with open(log_path, "ab") as log_f:
                # Override TELEGRAM_STATE_DIR to worktree's .telegram so the
                # subagent doesn't inherit the crew server's state dir and steal
                # the coordinator bot's Telegram connection.
                # Strip PYTHONPATH/PYTHONHOME so codex/gemini python wrappers
                # don't load the server's 3.10 stdlib under a 3.12 interpreter
                # (causes "SRE module mismatch" crash on subprocess startup).
                _dispatch_env = {**os.environ, "TELEGRAM_STATE_DIR": os.path.join(wt, ".telegram")}
                _dispatch_env.pop("PYTHONPATH", None)
                _dispatch_env.pop("PYTHONHOME", None)
                # start_new_session=True puts proc in its own process group so
                # we can kill the whole tree on timeout — agent CLIs (gemini,
                # agy, codex) spawn helper children that survive a plain
                # proc.kill() and reparent to PID 1 as orphans (#191).
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=log_f, stderr=log_f, cwd=wt, env=_dispatch_env,
                    start_new_session=True,
                )
            _timed_out = False
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout_secs)
            except asyncio.TimeoutError:
                _timed_out = True
                # Kill the entire process group, not just the direct child.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                logger.error(f"dispatcher: timeout {timeout_secs}s task={task.task_id}")
            # Inspect the dispatch log tail for upstream errors — applies to
            # both clean exit AND timeout (#190). Claude can return rc=0 with
            # api_error_status:429; gemini-cli often hangs on retry loops past
            # the 15-minute timeout. Both need the same routing decision.
            _transient = _detect_transient_error_in_log(
                log_path, since_offset=_task_log_start_offset
            )
            # #236: `subscriber fell behind updates` is usually a MASK. agy
            # hits 429, its internal retry stalls the agent_state pubsub
            # subscriber, agy kills the subscriber, and only that downstream
            # line reaches us. #232 measured 0/414 recoveries for this tag —
            # retrying it 3 more times cost 1,375 wasted attempts and ~225s
            # median added time-to-fail, while `agy_quota_exhausted` is
            # already correctly classified non-retriable.
            #
            # ⚠️Only reclassify when a 429 is actually correlated in agy's own
            #   log inside this task's window. #232 could not attribute ~25%
            #   of lag events, and those keep the retriable tag — we do not
            #   get to claim every lag event is a quota event.
            if _transient == "agy_subscriber_lag" and agy_quota_correlated(
                _task_started_wall, time.time() + 1.0
            ):
                logger.warning(
                    "dispatcher: task=%s reported agy_subscriber_lag but agy's "
                    "own log shows a preceding 429 — reclassifying as "
                    "agy_quota_exhausted (non-retriable) (#236)", task.task_id,
                )
                _transient = "agy_quota_exhausted"
            # #202: best-effort provider_session_id capture + compaction
            # detection, scoped to just this task's own output (same
            # since_offset technique as #200, so a previous task's leftover
            # text can't bleed into it). Both are observational — a miss
            # doesn't mean anything went wrong, just that nothing reliable
            # was observed on stdout for this provider.
            try:
                with open(log_path, "r", errors="replace") as _lf:
                    _lf.seek(_task_log_start_offset)
                    _task_log_tail = _lf.read()
                if agent == "claude":
                    _discovered_session_id = extract_claude_session_id(_task_log_tail)
                    if _discovered_session_id and _discovered_session_id != _ctx_info.get("provider_session_id"):
                        q().update_context_provider_session_id(_context_key, _discovered_session_id)
                elif agent == "codex":
                    # #262: close the loop. The run just now wrote a rollout for
                    # THIS worktree, so it is at the top of codex's newest day —
                    # cheap to find, and once recorded the next resume needs no
                    # search at all. Without this a fresh codex task would never
                    # acquire a binding and every dispatch would start over.
                    _codex_after = codex_session_for_cwd(wt)
                    if _codex_after and _codex_after != _ctx_info.get("provider_session_id"):
                        q().update_context_provider_session_id(_context_key, _codex_after)
                        logger.info(
                            "dispatcher: recorded codex session %s for %s (#262)",
                            _codex_after[:8], wt)
                if detect_context_compaction(_task_log_tail):
                    record_context_event(
                        _context_events_path, "context_compacted",
                        task_id=task.task_id, project=_project, role=role, agent=agent,
                        context_id=_ctx_info["context_id"],
                    )
            except Exception:
                logger.exception(f"dispatcher: context observation failed for task={task.task_id}")
            try:
                q().record_task_telemetry(
                    task.task_id, response_log_telemetry(agent, _task_log_tail))
                _telemetry_attr = q().get_attribution(task.task_id)
                if _telemetry_attr:
                    append_attribution_jsonl(_attr_jsonl_path, _telemetry_attr)
            except Exception:
                logger.exception("dispatcher: terminal telemetry enrichment failed for task=%s", task.task_id)
            if _transient in _TRANSIENT_RETRIABLE_TAGS:
                _n = _transient_retries.get(task.task_id, 0) + 1
                _transient_retries[task.task_id] = _n
                if _n <= _MAX_TRANSIENT_RETRY:
                    try:
                        q().requeue(task.task_id)
                        logger.warning(
                            f"dispatcher: transient {_transient} on "
                            f"task={task.task_id} — requeued "
                            f"(attempt {_n}/{_MAX_TRANSIENT_RETRY})"
                        )
                        _terminal = False
                        return
                    except Exception:
                        logger.exception(
                            f"dispatcher: requeue failed for task={task.task_id}"
                        )
                else:
                    logger.error(
                        f"dispatcher: transient {_transient} on "
                        f"task={task.task_id} — giving up after "
                        f"{_MAX_TRANSIENT_RETRY} retries"
                    )
                _fail_if_active(task.task_id, f"transient_{_transient}_max_retries")
            elif _transient in _TRANSIENT_NONRETRIABLE_TAGS:
                # Quota / tier failures — won't recover in minutes, no point
                # retrying. Surface the cause clearly so operators see why
                # the task died (#192).
                logger.error(
                    f"dispatcher: {_transient} on task={task.task_id} — "
                    "failing without retry (quota reset / migration required)"
                )
                _fail_if_active(task.task_id, _transient)
            elif _timed_out:
                # #265: NOT `failed`. The dispatcher stopped waiting; the worker
                # may still be running and may still POST — measured on
                # alpha_engine, six tasks in one day were marked failed and later
                # turned completed, with their commits already pushed. A consumer
                # reading status at notification time saw a false failure and
                # would have re-issued work that was already done (including one
                # task that had already opened a PR).
                _fail_if_active(task.task_id, "dispatcher_timeout", status="timed_out")
            elif proc.returncode != 0:
                _fail_if_active(task.task_id, f"exit_{proc.returncode}")
            else:
                # Same reasoning: the process is gone without a result, but the
                # POST can still be in flight. "We did not observe a result" is
                # not "the work failed".
                _fail_if_active(task.task_id, "no_result_submitted", status="timed_out")
        except Exception:
            logger.exception(f"dispatcher: error task={task.task_id}")
            _fail_if_active(task.task_id, "dispatcher_exception")
        finally:
            # #272: release the test-stage lock before anything else in the
            # teardown can raise — a leaked flock would block every later test
            # on this worktree until the process exits.
            _lock_stack.close()
            # Pop the per-task transient-retry counter once the task reaches
            # a terminal outcome, so the in-memory dict doesn't grow
            # unbounded across long-running servers (#194). Left in place
            # (_terminal=False) across a successful requeue so the count
            # actually accumulates across retries (#201).
            if _terminal:
                _transient_retries.pop(task.task_id, None)
            # Rotate dispatch logs and the attribution ledger when they cross
            # the cap (#193). Append-only paths grew to >100MB in production.
            try:
                _log_cap_mb = int(os.getenv("AGENT_CREW_LOG_MAX_MB", "50"))
            except ValueError:
                _log_cap_mb = 50
            _rotate_log_if_oversized(log_path, _log_cap_mb)
            _rotate_log_if_oversized(
                os.path.join(os.path.dirname(db_path), "attribution.jsonl"),
                _log_cap_mb,
            )
            # Reset worktree after task (success or failure) so it's clean for the next task.
            # The cleanup wipes agent_crew's per-role protocol files (.claude/CLAUDE.md
            # is untracked → `git clean -fd` removes it; AGENTS.md / GEMINI.md are often
            # tracked → `git checkout .` reverts the agent_crew block back to the
            # project's committed version). Both paths break the implementer's review
            # delegation flow (issue #187). After the reset we re-write all role
            # protocol files so the next dispatch finds them intact.
            try:
                subprocess.run(
                    ["git", "-C", wt, "checkout", "."],
                    capture_output=True,
                )
                subprocess.run(
                    [
                        "git", "-C", wt, "clean", "-fd",
                        "-e", ".claude/CLAUDE.md",
                        "-e", "AGENTS.md",
                        "-e", "GEMINI.md",
                    ],
                    capture_output=True,
                )
                logger.debug(f"dispatcher: worktree reset after task={task.task_id} role={role}")
            except Exception:
                logger.exception(f"dispatcher: worktree reset failed for {role} task={task.task_id}")
            # Re-apply agent_crew protocol files for every role we host. Idempotent —
            # implementer's .claude/CLAUDE.md is overwritten; AGENTS.md/GEMINI.md get
            # the marker block re-merged onto the project's content.
            try:
                _port_file = os.path.join(os.path.dirname(db_path), "port")
                if os.path.exists(_port_file):
                    _proj = (
                        task.project
                        or os.path.basename(os.path.dirname(db_path))
                        or "project"
                    )
                    for _r, _wt in worktree_map.items():
                        if not _wt:
                            continue
                        _agent = _DISPATCH_ROLE_TO_AGENT.get(_r, "")
                        instructions.write(
                            _r, _wt, _proj, _port_file,
                            agent=_agent, delivery="dispatcher",
                        )
            except Exception:
                logger.exception(
                    f"dispatcher: protocol re-write failed after task={task.task_id} role={role}"
                )

    async def _dispatcher_loop() -> None:
        """Poll the DB every AGENT_CREW_DISPATCH_INTERVAL seconds and spawn
        headless agent subprocesses.

        ⭐**동시성 키는 실행 자원이다 — `task_type` 이 아니다.**

        2026-09-23 실측(alpha_engine `:8101`): pane 세 개가 전부 비어 있는데도
        `implement` 두 건이 직렬로 돌았다. 루프가 `for role in (...)` 로 **역할
        슬롯**을 잠갔고 `agent_override` 는 그 뒤 실행 대상을 고를 때만 읽혔기
        때문이다 — override 로 다른 worker 에 보낸 두 번째 implement 가
        **스케줄 단계에서** 막혔다. 실효 병렬도가 3이 아니라 1이었다.

        이제 잠그는 것은 두 가지뿐이다:

        - **execution slot(`worker_id`)** — 같은 worker 에 동시에 두 task 금지.
          한 provider 의 `--continue` 세션을 둘이 나눠 쓸 수 없다.
        - **worktree lease** — 같은 worktree 에 동시 writer 금지(#202 / PR #203
          finding 1). 서로 다른 worker 라도 override 로 같은 worktree 에 겹칠 수 있다.

        ⛔`task_type`·`role` 은 잠금 키가 아니다. 서로 다른 worker + 서로 다른
        worktree 면 같은 task_type 도 병렬로 돈다.

        ⛔slot 은 **pane 이 아니라 `worker_id`** 다. pane 은 전달 계층의 관심사이고
        여기서는 쓰지 않는다 — tmux 가 아닌 worker backend(컨테이너·원격 러너)가
        추가돼도 이 스케줄러를 다시 뜯지 않게 하기 위해서다.
        """
        active_workers: set[str] = set()      # execution slot lease: worker_id
        active_worktrees: set[str] = set()    # worktree lease: 해석된 worktree 경로
        active_tasks: dict[str, asyncio.Task] = {}   # task_id → asyncio.Task
        task_slots: dict[str, str] = {}       # task_id → worker_id

        # agent → 그 agent 가 기본으로 맡는 role (worktree 조회·기본 라우팅용).
        _AGENT_TO_ROLE: dict[str, str] = {}
        _ROLE_PRIORITY = {"implementer": 0, "reviewer": 1, "tester": 2}
        for _role, _agent in _DISPATCH_ROLE_TO_AGENT.items():
            current = _AGENT_TO_ROLE.get(_agent)
            if current is None or _ROLE_PRIORITY.get(_role, 99) < _ROLE_PRIORITY.get(current, 99):
                _AGENT_TO_ROLE[_agent] = _role

        # worker 순회 순서 — 역할 우선순위를 따르되 같은 worker 는 한 번만.
        _workers: list[str] = []
        for _role, _agent in sorted(
            _DISPATCH_ROLE_TO_AGENT.items(),
            key=lambda kv: _ROLE_PRIORITY.get(kv[0], 99),
        ):
            if _agent and _agent not in _workers:
                _workers.append(_agent)
        # discuss 는 role 매핑에 없는 agent 로도 올 수 있다.
        _discuss_workers = list(dict.fromkeys(_workers + ["claude", "codex", "gemini"]))

        # ⭐dispatcher 가 **실제로 들고 있는 lease**. orphan 판정의 권위다 —
        #   `in_progress` 인데 여기 없으면 주인이 사라진 task 다(GET /tasks/orphans).
        app.state.dispatcher_active_tasks = active_tasks
        app.state.dispatcher_active_workers = active_workers
        app.state.dispatcher_active_worktrees = active_worktrees

        interval = float(os.getenv("AGENT_CREW_DISPATCH_INTERVAL", "2"))
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    logger.debug(
                        f"dispatcher: loop tick worktree_map_keys={list(worktree_map.keys())} "
                        f"active_workers={sorted(active_workers)} "
                        f"active_worktrees={len(active_worktrees)}"
                    )
                    # Reap completed dispatches, freeing their slots.
                    done = [tid for tid, t in list(active_tasks.items()) if t.done()]
                    for tid in done:
                        active_tasks.pop(tid, None)
                        slot = task_slots.pop(tid, None)
                        if slot:
                            active_workers.discard(slot)

                    for worker in _workers:
                        if worker in active_workers:
                            continue
                        _default_role = _AGENT_TO_ROLE.get(worker, "")
                        # ⭐`dequeue(agent=…)` 의 1단계가 **task_type 과 무관하게**
                        #   `context.agent_override == worker` 인 task 를 먼저 집는다.
                        #   override 가 스케줄 단계에서 반영되는 지점이 여기다.
                        task = q().dequeue(agent=worker, role=_default_role)
                        if task is None:
                            continue
                        # 프로토콜·결과 처리는 task_type 이 정한다.
                        # override 는 **실행 자원만** 바꾼다(역할을 바꾸지 않는다).
                        role = _TYPE_TO_ROLE.get(
                            task.task_type, _default_role or "implementer")
                        _target_agent, _target_wt = _resolve_dispatch_target(task, role)
                        _slot = _target_agent or worker
                        if _slot in active_workers:
                            # override 가 이미 바쁜 worker 를 가리켰다.
                            logger.info(
                                f"dispatcher: deferring task={task.task_id} — execution slot "
                                f"{_slot} is held (task_type={task.task_type})"
                            )
                            q().requeue(task.task_id)
                            continue
                        if _target_wt and _target_wt in active_worktrees:
                            # 같은 worktree 에 두 writer 를 붙이지 않는다.
                            logger.info(
                                f"dispatcher: deferring task={task.task_id} role={role} "
                                f"agent={_target_agent} — worktree lease {_target_wt} is held"
                            )
                            q().requeue(task.task_id)
                            continue
                        active_workers.add(_slot)
                        if _target_wt:
                            active_worktrees.add(_target_wt)
                        task_slots[task.task_id] = _slot

                        async def _run(
                            t: TaskRequest = task, r: str = role, s: str = _slot,
                            w: Optional[str] = _target_wt,
                        ) -> None:
                            try:
                                await _dispatch_task(t, r)
                            finally:
                                active_workers.discard(s)
                                if w:
                                    active_worktrees.discard(w)
                                active_tasks.pop(t.task_id, None)
                                task_slots.pop(t.task_id, None)

                        active_tasks[task.task_id] = asyncio.create_task(_run())

                    # discuss 도 **같은 execution slot 네임스페이스**를 쓴다.
                    # ⛔예전에는 `discuss_<agent>` 라는 별도 키였다 — 같은 provider 에
                    #   일반 task 와 discuss 가 동시에 붙을 수 있었다(같은 결함의 변종).
                    for agent in _discuss_workers:
                        if agent in active_workers:
                            continue
                        task = q().dequeue_discuss_for_agent(agent)
                        if task is None:
                            continue
                        role = _AGENT_TO_ROLE.get(agent, "implementer")
                        _target_agent, _target_wt = _resolve_dispatch_target(task, role)
                        _slot = _target_agent or agent
                        if _slot in active_workers:
                            logger.info(
                                f"dispatcher: deferring discuss task={task.task_id} "
                                f"— execution slot {_slot} is held"
                            )
                            q().requeue(task.task_id)
                            continue
                        if _target_wt and _target_wt in active_worktrees:
                            logger.info(
                                f"dispatcher: deferring discuss task={task.task_id} agent={agent} "
                                f"— worktree lease {_target_wt} is held"
                            )
                            q().requeue(task.task_id)
                            continue
                        active_workers.add(_slot)
                        if _target_wt:
                            active_worktrees.add(_target_wt)
                        task_slots[task.task_id] = _slot

                        async def _run_discuss(
                            t: TaskRequest = task, r: str = role, s: str = _slot,
                            w: Optional[str] = _target_wt,
                        ) -> None:
                            try:
                                await _dispatch_task(t, r)
                            finally:
                                active_workers.discard(s)
                                if w:
                                    active_worktrees.discard(w)
                                active_tasks.pop(t.task_id, None)
                                task_slots.pop(t.task_id, None)

                        active_tasks[task.task_id] = asyncio.create_task(_run_discuss())
                except Exception:
                    logger.exception("dispatcher loop raised — continuing")
        except asyncio.CancelledError:
            for t in active_tasks.values():
                t.cancel()
            return

    app.state.dispatcher_enabled = _dispatcher_enabled
    # Same rationale as watchdog_tick/anomaly_tick above: expose the dispatch
    # path so a test can drive one real dispatch deterministically, rather
    # than asserting against a helper the dispatcher may not actually call.
    # Its absence is why PR #241 shipped a Context Pack that silently omitted
    # the acceptance criteria on every live dispatch while unit tests passed.
    # Exposed for integration tests and operational introspection of the exact
    # provider/worktree decision the dispatcher will use.
    app.state.resolve_dispatch_target = _resolve_dispatch_target
    app.state.dispatch_task = _dispatch_task
    # Same rationale (#248, #265): expose the terminal-marking helper so a test
    # can drive the real timeout path instead of asserting against a
    # reimplementation of it.
    app.state.fail_if_active = _fail_if_active
    # ── End headless dispatcher ───────────────────────────────────────────────

    def _auto_enqueue_review(
        impl_task_id: str,
        pr_number: Optional[int] = None,
        result=None,
    ) -> None:
        """HTTP-side wrapper: run the transport-agnostic cascade then push.

        The body of the cascade lives in ``agent_crew.pipeline`` so the MCP
        ``submit_result`` path can fire the same hook (#123). This wrapper
        only adds the tmux push side-effect, which the MCP path skips —
        agents on the MCP loop pull tasks themselves.
        """
        review_id = _pipeline_auto_enqueue_review(
            q(),
            impl_task_id,
            pr_number,
            pane_map=pane_map,
            server_project=project,
            # #305: the implementer's own report of where it pushed. Without it
            # the cascade can only route from the task, which for a
            # watch-ingested issue is `main`.
            result=result,
            repo_cwd=_any_worktree_path(),
        )
        if review_id:
            _try_push_next("reviewer")

    def _auto_enqueue_test(review_task_id: str, repo: str = "") -> None:
        """HTTP-side wrapper: run the transport-agnostic cascade then push.
        See ``_auto_enqueue_review`` for the rationale (#123)."""
        test_id = _pipeline_auto_enqueue_test(
            q(),
            review_task_id,
            pane_map=pane_map,
            repo=repo,
            repo_cwd=_any_worktree_path(),
        )
        if test_id:
            _try_push_next("tester")

    def _any_worktree_path() -> str:
        """A worktree that is a checkout of THIS project's repository.

        Used only to resolve the repo slug for `gh`. Any role's worktree will
        do — they are all checkouts of the same repository — and "" is returned
        when none is configured, which callers treat as "repository unknown".
        """
        try:
            for role in ("implementer", "reviewer", "tester"):
                path = (worktree_map or {}).get(role) or ""
                if path and os.path.isdir(os.path.join(path, ".git")):
                    return path
                if path and os.path.exists(os.path.join(path, ".git")):
                    return path            # linked worktree: .git is a file
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _requeue_review_at_head(review_task_id: str, pr_number, head: str, ctx) -> None:
        """Enqueue one head-anchored review for a PR whose head moved (#304).

        ⛔The task id is derived from the PR and the new head, so two results
          observing the same move produce ONE review rather than two reviewers
          on one commit — the same claim-by-primary-key mechanism as #244's fix
          ids. A head that moves again is a genuinely different review and gets
          its own id.
        """
        new_id = stale_review_task_id(pr_number, head)
        base = ctx if isinstance(ctx, dict) else {}
        context = {k: v for k, v in base.items()
                   if k in ("pr_number", "repo", "project", "no_tester",
                            "coordinator_managed", "checklist_layers")}
        context.update({"pr_number": int(pr_number), "superseded_review": review_task_id,
                        "expected_head_sha": head})
        try:
            q().enqueue(TaskRequest(
                task_id=new_id,
                task_type="review",
                description=f"Review PR #{pr_number} at {head[:12]} "
                            f"(re-dispatched: {review_task_id} reviewed an older head)",
                branch=base.get("branch") or "main",
                priority=3,
                context=context,
            ))
            logger.info(
                f"_requeue_review_at_head: enqueued {new_id} for PR #{pr_number} at "
                f"{head[:12]}, superseding {review_task_id} (#304)")
        except TaskAlreadyExistsError:
            logger.info(
                f"_requeue_review_at_head: {new_id} already exists — another result "
                f"observed the same head; not duplicating it (#304)")
        except _PausedError:
            # #314 §4 P0: STOP race — 다른 successor helper와 동일하게 부모(review) outbox를
            # reopen해 재개 후 result-carrying replay로 이 stale-review 재dispatch를 복구하고 전파.
            try:
                q().outbox_reopen(review_task_id)
            except Exception:
                logger.exception(f"_requeue_review_at_head: outbox_reopen({review_task_id}) 실패")
            raise
        except Exception:  # noqa: BLE001
            logger.exception(
                f"_requeue_review_at_head: could not requeue a review for PR "
                f"#{pr_number} at {head[:12]}")

    def _auto_enqueue_fix(review_task_id: str, repo: str = "") -> None:
        """HTTP-side wrapper: run the transport-agnostic cascade then push.
        See ``_auto_enqueue_review`` for the rationale (#123, #244)."""
        # The cascade swallows its own errors, but the invariant it is
        # protecting belongs here: a result submission must never 500 because
        # a FOLLOW-UP failed. The agent has already done the work, and an
        # agent that cannot POST its result is a task the dispatcher marks
        # failed on timeout.
        try:
            fix_id = _pipeline_auto_enqueue_fix(
                q(),
                review_task_id,
                pane_map=pane_map,
                server_project=project,
                repo=repo,
                # #253 review: name the repository. This process's cwd is the
                # instance directory, which belongs to a DIFFERENT repo, so any
                # `gh` call that infers from it asks the wrong one. An agent
                # worktree is a checkout of the right repository, so it is the
                # correct place to resolve from when the task context carries
                # no explicit `repo`.
                repo_cwd=_any_worktree_path(),
                # #314 §4 P0: replay 중엔 fix-budget exhaustion PR comment skip.
                suppress_side_effects=_REPLAYING.get(),
            )
            if fix_id:
                _try_push_next("implementer")
        except _PausedError:
            # #314 §4 P0-2: pipeline이 이미 outbox reopen했음 — handler suppressed 200 위해 전파.
            raise
        except Exception:
            logger.exception(
                f"_auto_enqueue_fix: cascade failed for {review_task_id} — "
                f"the review result stands, the fix was not enqueued"
            )

    def _auto_retry_failed_task(task_id: str, result: TaskResult, task_type: str) -> None:
        """Auto-retry a failed task if it hasn't exceeded max retries.
        This provides resilience against transient failures."""
        MAX_RETRIES = 2
        try:
            # Get the original task to extract description, branch, and context
            tasks = [t for t in q().list_tasks() if t.task_id == task_id]
            if not tasks:
                return
            original_task = tasks[0]

            # #167: use the DB context retry_attempt, not result.retry_count.
            # Agents always submit retry_count=0 (they don't track it); the DB
            # context is the authoritative source of how many times this chain
            # has been retried.
            db_retry_attempt = (
                original_task.context.get("retry_attempt", 0)
                if isinstance(original_task.context, dict)
                else 0
            )
            if db_retry_attempt >= MAX_RETRIES:
                logger.info(
                    f"Task {task_id} failed (status={result.status}), "
                    f"but DB retry_attempt={db_retry_attempt} >= MAX_RETRIES={MAX_RETRIES}"
                )
                return

            # #161: review tasks with no branch AND no pr_number have no way to
            # locate a PR — retrying will produce the same failure. Abort early
            # to prevent the review-retry loop.
            if task_type == "review":
                task_ctx = original_task.context if isinstance(original_task.context, dict) else {}
                if not original_task.branch and not task_ctx.get("pr_number"):
                    logger.warning(
                        f"_auto_retry_failed_task: skipping review retry for {task_id} "
                        f"— no branch and no pr_number; retry would loop (#161)"
                    )
                    return
                # #216: a branch IS set here, but if it genuinely has no PR
                # (open or closed), a retry re-dispatches the exact same
                # branch to the exact same "gh pr list" dead end — the agent
                # rediscovers "no PR" itself, burning a full invocation to
                # relearn what the dispatcher can check in one cheap `gh pr
                # list` call. Observed live: 2 review tasks each retried
                # twice (4 wasted attempts total) against branches with no
                # PR the whole time.
                if original_task.branch and not task_ctx.get("pr_number"):
                    from agent_crew.github import branch_has_pr
                    if not branch_has_pr(original_task.branch):
                        logger.warning(
                            f"_auto_retry_failed_task: skipping review retry for {task_id} "
                            f"— branch {original_task.branch!r} has no PR (open or closed); "
                            f"retry would hit the same dead end (#216)"
                        )
                        return

            # Create retry task with incremented retry count
            retry_context = dict(original_task.context) if isinstance(original_task.context, dict) else {}
            retry_context.pop(RESULT_BRANCH_CONTEXT_KEY, None)
            retry_context.pop(RESULT_COMMIT_CONTEXT_KEY, None)
            retry_context["retry_attempt"] = db_retry_attempt + 1
            retry_context["original_task_id"] = task_id

            # #314 §4 P0-1: 결정론 successor id — retry attempt로 구분(replay가 같은 attempt→같은 id).
            retry_req = TaskRequest(
                task_id=f"retry-{task_id}-a{db_retry_attempt + 1}",
                task_type=task_type,  # type: ignore
                description=original_task.description,
                branch=original_task.branch,
                priority=original_task.priority + 1,  # Bump priority for retries
                context=retry_context,
            )
            from agent_crew.queue import TaskAlreadyExistsError as _TAE
            try:
                q().enqueue(retry_req)
            except _TAE:
                logger.info(f"_auto_retry_failed_task: {retry_req.task_id} 이미 존재 — 멱등 skip")
            logger.info(f"Task {task_id} auto-retried (attempt {result.retry_count + 1}/{MAX_RETRIES})")
            # Try to push the retry task
            role = _TYPE_TO_ROLE.get(task_type)
            if role:
                _try_push_next(role)
        except _PausedError:
            # #314 §4 P0-2: STOP race — 부모(failed task) outbox reopen + 전파(handler suppressed 200).
            try:
                q().outbox_reopen(task_id)
            except Exception:
                logger.exception(f"_auto_retry_failed_task: outbox_reopen({task_id}) 실패")
            raise
        except Exception as e:
            logger.warning(f"Failed to auto-retry task {task_id}: {e}")
            pass

    def _auto_merge_pr(pr_number: int, repo: str = "", repo_cwd: str = "") -> None:
        """Merge PR via gh CLI after the pipeline approves it (#171).

        Failures are logged and swallowed — a merge error must never break
        the result-submission response.
        """
        # #314 재리뷰: STOP 확인과 merge 시작을 원자화한다. external_op_reserve가 같은 BEGIN
        # IMMEDIATE 트랜잭션에서 runtime_stop을 확인해 unpaused일 때만 admit+reserve → STOP이
        # reservation보다 먼저 linearize되면 admitted=False로 차단(별도 STOP 체크와 reserve 사이의
        # TOCTOU 제거). §5: crash(merge 후 done 전)는 재기동 시 reserved 보고 pr_state 재확인.
        from agent_crew.github import get_repo, merge_pr, pr_state
        _merge_repo = repo or (get_repo(cwd=repo_cwd) if repo_cwd else "") or ""
        op_key = f"merge:pr:{pr_number}"
        resv = q().external_op_reserve(op_key, pr_number=int(pr_number))
        if not resv.get("admitted"):
            logger.warning(f"[PAUSE-SUPPRESSED] _auto_merge_pr(#{pr_number}) 억제 — "
                           f"STOP admission 거부({resv.get('state')})")
            return
        if resv.get("state") == "done":
            logger.info(f"_auto_merge_pr: {op_key} 이미 done(receipt) — merge 재실행 안 함")
            return
        # 비가역 실패 누적(conflict/closed 등)은 자동 재시도 안 함 → escalation 대상.
        if resv.get("state") == "failed" and int(resv.get("attempt", 0)) >= _MAX_MERGE_ATTEMPTS:
            logger.warning(f"_auto_merge_pr: {op_key} 실패 {resv.get('attempt')}회(≥{_MAX_MERGE_ATTEMPTS}) — "
                           f"자동 재시도 중단, escalation 필요(last_error={resv.get('last_error')})")
            return
        if not _merge_repo:
            q().external_op_mark(
                op_key, "failed",
                last_error="repo identity unresolved (no explicit repo, no "
                           "project worktree) — fail-closed, no merge attempted",
                inc_attempt=True)
            logger.warning(f"_auto_merge_pr: repo identity 미확정 — merge 억제(PR #{pr_number}, "
                           f"mutation 0)")
            return
        # reconciliation: 맹목 재실행이 아니라 실제 상태를 먼저 확인.
        st = pr_state(int(pr_number), repo=_merge_repo)
        if st == "merged":
            q().external_op_mark(op_key, "done")
            logger.info(f"_auto_merge_pr: PR #{pr_number} 이미 merged(재확인) → done 기록, 재merge 안 함")
            return
        if st == "closed":
            q().external_op_mark(op_key, "failed",
                                 last_error="PR closed(비가역) — 자동 merge 불가", inc_attempt=True)
            logger.warning(f"_auto_merge_pr: PR #{pr_number} closed — 자동 재시도 안 함, escalation 필요")
            return
        if st == "unknown":
            q().external_op_mark(op_key, "failed",
                                 last_error="PR 상태 불명(gh 실패) — 다음 재확인 대기", inc_attempt=True)
            logger.warning(f"_auto_merge_pr: PR #{pr_number} 상태 불명 → merge 보류(fail-closed, 재확인)")
            return
        # st == 'open' → merge 시도
        ok = merge_pr(int(pr_number), merge_method="squash", repo=_merge_repo)
        if ok:
            q().external_op_mark(op_key, "done")
            logger.info(f"_auto_merge_pr: merged PR #{pr_number} (squash) → done(receipt) — #171/§5")
        else:
            q().external_op_mark(op_key, "failed",
                                 last_error="gh pr merge 실패", inc_attempt=True)
            logger.warning(f"_auto_merge_pr: gh pr merge #{pr_number} 실패 — failed 기록(재시도 가능)")

    def _auto_fallback_failed_task(
        task_id: str,
        result: TaskResult,
        task_type: str,
    ) -> bool:
        """HTTP-side wrapper around the transport-agnostic fallback hook.

        The decision logic moved to ``agent_crew.pipeline.auto_fallback_failed_task``
        so MCP can call the same path (#123). Push side-effect stays
        HTTP-only — when fallback enqueues a successor task, nudge that
        role's pane.
        """
        handled = _pipeline_auto_fallback_failed_task(
            q(),
            task_id,
            result,
            task_type,
            pane_map=pane_map,
            state_path=state_path,
            fallback_disabled=bool(fallback_disabled),
            # #314 §4 P0: replay 중엔 escalation gate/telegram 같은 side effect skip(중복 방지).
            suppress_side_effects=_REPLAYING.get(),
        )
        if handled:
            role = _TYPE_TO_ROLE.get(task_type)
            if role:
                # No-op when no successor was enqueued (escalation path).
                _try_push_next(role)
        return handled

    def _server_identity() -> dict:
        """Which dispatcher is this? (#248 AC1)

        ⛔`project` cannot be taken from the `create_app` argument alone: the
          module-level `app` every live server is launched from never passes it,
          so all four dispatchers would report `project=""` and the provenance
          gate could not tell them apart. The state directory is the identity
          that actually exists in production — `~/.agent_crew/<project>/tasks.db`
          — so fall back to it, and report the paths alongside so identity is
          unambiguous even when an inherited AGENT_CREW_PORT is misleading.
        """
        name = project or ""
        if not name and db_path:
            parent = os.path.basename(os.path.dirname(os.path.abspath(db_path)))
            if parent and parent != ".agent_crew":
                name = parent
        return {"project": name, "db_path": db_path, "port": port or 0,
                "state_path": state_path or ""}

    @app.get("/health")
    def health():
        """Liveness plus the build this process is actually running (#248).

        The provenance rides on /health deliberately: the thing that polls a
        server to see whether it is up is the thing that should notice it is up
        on the wrong code. #247's gap survived because "the server is running"
        and "the server is running the merged fix" were separate questions and
        only the first one had an answer.
        """
        ident = _server_identity()
        snap = _prov.snapshot(project=ident["project"], port=ident["port"])
        # #314 §6: 이 런타임이 실제로 DB(runtime_stop)에서 읽은 STOP 상태를 노출한다. fleet_stop이
        # post-restart ACK를 판정할 때 pause.json만이 아니라 "새 build가 DB stop epoch/incident를
        # 읽어 paused로 올라왔는지"를 확인할 수 있어야 canary 자격이 생긴다. 조회 불가는 fail-closed(paused).
        try:
            _stop = q().get_stop_epoch()
            _stop_out = {"epoch": _stop.get("epoch"), "paused": bool(_stop.get("paused")),
                         "incident": _stop.get("incident")}
        except Exception:
            _stop_out = {"epoch": None, "paused": True, "incident": None, "error": "read_failed"}
        return {
            "status": "ok",
            "project": ident["project"],
            "identity": ident,
            "stop": _stop_out,
            "build": {
                "commit": snap["commit"],
                "commit_short": snap["commit_short"],
                "ref": snap["ref"],
                "dirty": snap["dirty"],
                "code_fingerprint": snap["code_fingerprint"],
                "package_version": snap["package_version"],
                "started_at": snap["started_at"],
                "uptime_s": snap["uptime_s"],
                "pid": snap["pid"],
                "source_root": snap["source_root"],
                "checkout_commit": snap["checkout_commit"],
                "checkout_moved_since_start": snap["checkout_moved_since_start"],
                "source_changed_since_start": snap["source_changed_since_start"],
            },
        }

    @app.get("/runtime/coordinator")
    def get_runtime_coordinator():
        """Generic, project-scoped coordinator authority (#309 K1)."""
        return q().get_coordinator_state()

    @app.post("/runtime/coordinator/handoff")
    def handoff_runtime_coordinator(body: dict):
        """Advance coordinator authority with a generation CAS; never dispatches work."""
        required = ("coordinator_id", "generation")
        missing = [key for key in required if key not in body]
        if missing:
            raise HTTPException(status_code=422, detail=f"missing required coordinator fields: {missing}")
        result = q().advance_coordinator(
            coordinator_id=str(body["coordinator_id"]), generation=body["generation"],
            provider=str(body.get("provider") or "unknown"),
            model=str(body.get("model") or "unknown"),
            provider_session_id=str(body.get("provider_session_id") or "unknown"),
            handoff_reason=str(body.get("handoff_reason") or ""),
            previous_receipt_hash=str(body.get("previous_receipt_hash") or ""),
        )
        if not result["accepted"]:
            raise HTTPException(status_code=409, detail=result)
        return result

    @app.get("/runtime/export")
    def export_runtime_state():
        """Read-only successor-coordinator snapshot; no scheduler coupling (#309)."""
        return q().export_project_runtime_state()

    @app.get("/provenance")
    def provenance(expect: str = ""):
        """Full build provenance, optionally graded against an expected ref.

        `GET /provenance?expect=98d869d` answers "is this dispatcher running a
        build that contains #238?" — the question #247 had to answer by SSHing
        to the host and reading `git HEAD`, which is also the question a
        before/after measurement must gate on before labelling a cohort.

        ⛔Read-only by contract. It never pulls, restarts, or repairs anything;
          a safe-boundary deployment stays an operator action (#248).
        """
        ident = _server_identity()
        snap = _prov.snapshot(project=ident["project"], port=ident["port"])
        out = dict(snap)
        out["identity"] = ident
        if expect:
            out["expected"] = _prov.compare(expect, snap=snap)
        return out

    @app.post("/pane_map/reload")
    def reload_pane_map():
        """Re-read pane_map.json from disk and update routing in-place.
        Called by `crew setup` when panes are recreated without restarting the server."""
        new_pm = _load_pane_map()
        if new_pm is None:
            return {"status": "error", "message": "AGENT_CREW_PANE_MAP not set or file missing"}
        if pane_map is None:
            return {"status": "error", "message": "server pane_map is None (cannot update in-place)"}
        pane_map.clear()
        pane_map.update(new_pm)
        logger.info(f"pane_map reloaded: {pane_map}")
        return {"status": "ok", "pane_map": pane_map}

    @app.post("/tasks", status_code=201)
    def post_task(task: TaskRequest):
        """Enqueue a task.

        ``201`` → ``{"task_id": "..."}``.

        ``409`` → the ``task_id`` is already taken, with the existing row's
        status so a caller can tell "already running" from "already finished"
        without a second request (#273). ⛔The payload is NESTED under
        ``detail``, because it is raised as an ``HTTPException`` like every
        other error this server returns::

            {"detail": {"error": "task_id already exists",
                        "task_id": "...", "status": "pending"}}

        PR #274 documented a top-level body; that was never what went over the
        wire, and a client coding to it read ``None`` for every field. The
        envelope is the contract — a lone ``JSONResponse`` here would make this
        one endpoint's error shape unique — and
        ``tests/unit/test_issue_273_duplicate_task_id.py`` asserts the whole
        body so the description cannot drift from it again.
        """
        logger.info(f"POST /tasks: task_type={task.task_type}, task_id (will assign)...")
        # #294: gathered BEFORE the enqueue, so the task being created can
        # never appear in its own collision list.
        # ⛔Resolved the same way `enqueue` will resolve it moments later.
        #   Reading `context.issue` alone here meant a direct enqueue whose
        #   issue lives only in its description reported no collision and was
        #   then stored under that very issue (review of PR #295).
        _issue_number = task_issue_number(task)
        _in_flight: list = []
        if isinstance(_issue_number, int) and not isinstance(_issue_number, bool):
            try:
                _in_flight = active_tasks_for_issue(
                    q(), _issue_number, task_type=task.task_type)
            except Exception:
                # The whole feature is advisory; it must never cost a task.
                logger.exception(
                    f"POST /tasks: in-flight lookup failed for issue "
                    f"#{_issue_number} — enqueueing anyway")
                _in_flight = []
        try:
            task_id = q().enqueue(task)
        except TaskAlreadyExistsError as e:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "task_id already exists",
                    "task_id": e.task_id,
                    "status": e.status,
                },
            )
        logger.info(f"POST /tasks: enqueued task_id={task_id}")
        if _in_flight:
            # ⛔Advisory, never a gate. Several tasks legitimately share one
            #   issue — implement, its review, its fix rounds, its test — so
            #   refusing by issue would break the cascade. What was missing is
            #   only that nobody was TOLD: #294 measured a direct enqueue
            #   duplicating a watch task that was still in flight, 15 minutes
            #   before the first one's PR existed.
            logger.warning(
                f"POST /tasks: {task_id} is a second {task.task_type!r} task for "
                f"issue #{_issue_number} while {_in_flight} is still in flight. "
                f"Enqueued anyway — this is a heads-up, not a block (#294)."
            )
        if not _push_enabled:
            logger.warning(
                f"POST /tasks: AGENT_CREW_DELIVERY={_delivery_raw!r} — task {task_id} enqueued "
                "but tmux push is disabled; an MCP client must poll GET /tasks/next to receive it"
            )
        if task.task_type == "discuss":
            agent = task.context.get("agent") if isinstance(task.context, dict) else None
            logger.info(f"POST /tasks: discuss task, calling _try_push_discuss with agent={agent}")
            _try_push_discuss(agent)
        else:
            role = _TYPE_TO_ROLE.get(task.task_type)
            logger.info(f"POST /tasks: task_type={task.task_type} -> role={role}")
            if role:
                logger.info(f"POST /tasks: calling _try_push_next for role={role}")
                _try_push_next(role)
            else:
                logger.warning(f"POST /tasks: no role found for task_type={task.task_type}")
        # Always a list, never absent: a consumer should not have to tell "no
        # collision" from "this server does not report collisions".
        return {"task_id": task_id, "in_flight_for_issue": _in_flight}

    @app.get("/tasks/next")
    def get_next_task(role: str = "", agent: str = ""):
        # #172: in MCP-only mode the LLM must use the get_next_task MCP tool,
        # not curl-poll this HTTP endpoint — block to prevent idle token burn.
        if not _push_enabled:
            raise HTTPException(
                status_code=405,
                detail=(
                    f"HTTP task polling disabled (AGENT_CREW_DELIVERY={_delivery_raw!r}). "
                    "Use the MCP get_next_task tool instead of curl-polling this endpoint."
                ),
            )
        task = q().dequeue(agent=agent, role=role)
        if task is None:
            return None
        return task

    @app.get("/tasks")
    def list_tasks(status: str = ""):
        return q().list_tasks(status=status)

    def _dispatcher_lease_view() -> tuple[Optional[set], Optional[set]]:
        """dispatcher 가 지금 들고 있는 (task_id lease, worker slot) 을 돌려준다.

        ⛔dispatcher 가 안 돌고 있으면 `(None, None)` 이다 — 그때 "lease 가 없다"
          를 "주인이 사라졌다" 로 읽으면 **살아 있는 task 를 orphan 으로 오인**한다.
          그래서 호출부는 `lease_tracking` 을 함께 보고해야 한다.
        """
        _tasks = getattr(app.state, "dispatcher_active_tasks", None)
        _workers = getattr(app.state, "dispatcher_active_workers", None)
        if _tasks is None:
            return None, None
        return set(_tasks.keys()), set(_workers or ())

    @app.get("/tasks/orphans")
    def list_orphan_tasks(older_than: float = 0.0):
        """`in_progress` 인데 **주인(dispatcher lease)이 없는** task 를 보여준다.

        ⭐2026-09-23 실측: worker 프로세스가 사라졌는데 task 3건이 78분 동안
          `in_progress` 로 남아 레인을 잡았다. 그때 쓸 수 있던 API 는 **전역
          `expire-stale`** 뿐이라, 한 건을 치우려다 라이브 3건이 같이 취소됐다.
          이 조회는 아무것도 바꾸지 않는다 — 스윕 전에 대상과 건수를 먼저 본다.

        ⛔`lease_tracking=false` 면 이 목록은 orphan 판정이 아니라 **단순
          in_progress 목록**이다(dispatcher 미가동). 그 상태에서 recover 를
          돌리면 살아 있는 task 를 되돌릴 수 있다.
        """
        _leased, _ = _dispatcher_lease_view()
        _now = time.time()
        rows = q().list_in_progress_activity()
        out = []
        for r in rows:
            _ts = r["last_activity_at"] or r["created_at"]
            _age = max(0.0, _now - _ts) if _ts else None
            _orphan = (_leased is not None and r["task_id"] not in _leased)
            if _age is not None and _age < older_than:
                continue
            _ctx = r.get("context") or {}
            out.append({
                "task_id": r["task_id"],
                "task_type": r["task_type"],
                "idle_s": round(_age, 1) if _age is not None else None,
                "agent_override": (_ctx.get("agent_override") or None),
                "has_dispatcher_lease": (None if _leased is None
                                         else r["task_id"] in _leased),
                "orphan": _orphan,
            })
        return {
            "lease_tracking": _leased is not None,
            "in_progress": len(rows),
            "listed": len(out),
            "orphans": sum(1 for r in out if r["orphan"]),
            "older_than": older_than,
            "tasks": out,
        }

    @app.get("/tasks/{task_id}")
    def get_task(task_id: str):
        tasks = q().list_tasks()
        for t in tasks:
            if t.task_id == task_id:
                return t
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")

    @app.post("/tasks/{task_id}/result", status_code=200)
    def submit_result(task_id: str, result: TaskResult):
        logger.info(f"POST /tasks/{task_id}/result: status={result.status}")
        # Capture context before marking done — we need the agent name for
        # discuss-task follow-up pushes.
        ctx = q().get_task_context(task_id)
        _artifact_held = None
        _task = next((item for item in q().list_tasks() if item.task_id == task_id), None)
        try:
            _runtime_paused = bool(q().get_stop_epoch().get("paused")) or q()._pausejson_active()
        except Exception:
            # STOP state is safety authority.  Do not rewrite an in-flight
            # result while its state cannot be read; submit_result will retain
            # its established fail-closed cascade handling (#313).
            _runtime_paused = True
        _artifact_context = _task.context if _task is not None and isinstance(_task.context, dict) else {}
        if (not _runtime_paused and not _REPLAYING.get() and _task is not None
                and _task.task_type == "implement" and result.status == "completed"
                and not (_artifact_context.get("worktree_base_sha") or _artifact_context.get("reviewed_sha"))):
            logger.info("POST /tasks/%s/result: artifact gate not applied — dispatch base absent", task_id)
        if (not _runtime_paused and not _REPLAYING.get()
                and bool(_artifact_context.get("worktree_base_sha") or _artifact_context.get("reviewed_sha"))
                and _task is not None
                and _task.task_type == "implement" and result.status == "completed"):
            _ok, _detail = verify_implement_artifact(
                _task, result, repo_cwd=_any_worktree_path())
            if not _ok:
                _artifact_held = _detail
                result = no_artifact_result(result, _detail)
        # #268: does this result even claim to be about the PR we dispatched
        # it for? Must happen before the row is written, so what lands in the
        # DB is the held form — a human reading the row later sees the
        # disagreement, not a clean `completed` that quietly renamed its
        # target. Both numbers survive: `requested` in the context, `reported`
        # on the row.
        result, _pr_mismatch = hold_mismatched_pr_result(task_id, result, ctx)
        # #265: a result can arrive for a task the dispatcher already ended —
        # it stopped waiting, the worker kept going, and the row silently flips
        # from `timed_out` (previously `failed`) to `completed`. A consumer that
        # read the status when the notification fired sees only the first value
        # and never learns it was revised. Capture the prior status so the
        # revision can be announced.
        _prior = next((t.status for t in q().list_tasks() if t.task_id == task_id), "")
        try:
            task_type = q().submit_result(task_id, result)
            # #348: coordinator-managed loops consume the persisted result,
            # not this handler's in-memory object. Keep this deliberately
            # outside submit_result's STOP-atomic transaction: a telemetry-like
            # ref update must never alter result admission, cascade/outbox, or
            # pause suppression semantics.
            _result_ref = {
                key: value for key, value in (
                    (RESULT_BRANCH_CONTEXT_KEY, result.branch),
                    (RESULT_COMMIT_CONTEXT_KEY, result.commit),
                ) if value
            }
            if _result_ref:
                try:
                    q().merge_task_context(task_id, _result_ref)
                except Exception:
                    logger.exception(
                        "POST /tasks/%s/result: could not persist result ref metadata",
                        task_id,
                    )
            logger.info(f"POST /tasks/{task_id}/result: marked done, task_type={task_type}")
            if _prior in _LATE_RESULT_STATUSES and result.status != _prior:
                logger.warning(
                    f"POST /tasks/{task_id}/result: LATE RESULT — this task was "
                    f"already {_prior!r} and is now {result.status!r}. Anything that "
                    f"read the earlier status has a stale verdict (#265)."
                )
                try:
                    _attr = q().get_attribution(task_id)
                    record_context_event(
                        _context_events_path, "task_result_late",
                        task_id=task_id, previous_status=_prior,
                        new_status=result.status,
                        project=(_attr or {}).get("project"),
                        role=(_attr or {}).get("role"),
                        agent=(_attr or {}).get("agent"),
                        context_id=(_attr or {}).get("context_id"),
                    )
                except Exception:
                    logger.exception(
                        f"POST /tasks/{task_id}/result: late-result event failed")
        except ValueError as e:
            msg = str(e)
            logger.error(f"POST /tasks/{task_id}/result: error: {msg}")
            status_code = 404 if "not found" in msg.lower() else 400
            raise HTTPException(status_code=status_code, detail=msg)
        # #202: lifecycle event for the agent-self-reported terminal outcome
        # (the internal dispatcher-detected failure paths emit their own
        # task_failed from _fail_if_active — this covers the case where the
        # agent process itself completed and called back here).
        try:
            _attr = q().get_attribution(task_id)
            record_context_event(
                _context_events_path,
                "task_completed" if result.status == "completed" else "task_failed",
                task_id=task_id, outcome=result.status,
                project=(_attr or {}).get("project"),
                role=(_attr or {}).get("role"),
                agent=(_attr or {}).get("agent"),
                context_id=(_attr or {}).get("context_id"),
            )
            # #202 review finding 2: terminal-state line, not just the
            # dispatch-time snapshot (q().submit_result already set
            # status/outcome/completed_at on this row before we read it).
            if _attr:
                append_attribution_jsonl(_attr_jsonl_path, _attr)
                # #239: emit a compact episode at this safe boundary — the
                # task is terminal, so nothing is in flight. References and
                # metadata only; no prompt or source content is stored.
                try:
                    _ep_ctx = q().get_task_context(task_id) or {}
                    _cpack.append_episode(
                        os.path.join(os.path.dirname(db_path), "episodes.jsonl"),
                        _cpack.build_episode(
                            _attr,
                            {"summary": result.summary, "findings": result.findings,
                             "pr_number": result.pr_number},
                            issue=_ep_ctx.get("issue"),
                        ),
                    )
                except Exception:
                    logger.exception(
                        f"POST /tasks/{task_id}/result: episode emission failed")
        except Exception:
            logger.exception(f"POST /tasks/{task_id}/result: context event emission failed")
        if _pr_mismatch:
            # #268: the result is on the row and stays there. Everything that
            # would ACT on it stops — posting the verdict to a PR the reviewer
            # may never have read is the exact shape of alpha_engine#5288, and
            # enqueueing a fix/test/merge off it spends real work on a target
            # nobody has confirmed. `_try_push_next` is skipped too: the role
            # is idle, but the next thing it should get is a human's decision.
            _requested, _reported = _pr_mismatch
            logger.warning(
                f"POST /tasks/{task_id}/result: cascade stopped — dispatched for "
                f"PR #{_requested}, result reports PR #{_reported}. Result stored "
                f"as needs_human (#268)."
            )
            try:
                _attr = q().get_attribution(task_id)
                record_context_event(
                    _context_events_path, "task_result_pr_mismatch",
                    task_id=task_id, requested_pr=_requested, reported_pr=_reported,
                    task_type=task_type,
                    project=(_attr or {}).get("project"),
                    role=(_attr or {}).get("role"),
                    agent=(_attr or {}).get("agent"),
                    context_id=(_attr or {}).get("context_id"),
                )
            except Exception:
                logger.exception(
                    f"POST /tasks/{task_id}/result: pr-mismatch event failed")
            return {"status": "ok", "held": "pr_number_mismatch",
                    "requested_pr": _requested, "reported_pr": _reported}
        # #314 §3/§4 STOP: result는 이미 persist(marked done)돼 lineage/audit 보존됨.
        # 억제 판단은 queue.submit_result가 result 저장과 **같은 원자 txn**에서 확정해 cascade_outbox에
        # 기록했다(state='pending' → 억제 / 'applied' → 라이브 처리). 서버는 pause를 재확인하지 않고
        # 그 원자 결정(outbox state)만 신뢰한다 → 재확인 divergence 제거. outbox row 자체가 durable
        # suppression 기록이며(§3, result 저장과 원자적), 재개 후 executor(replay endpoint)가 drain한다.
        # fail-closed: outbox 조회 불가/부재는 억제로 간주.
        try:
            _ob = q().outbox_get(task_id)
        except Exception:
            _ob = None
        _suppressed = (_ob is None) or (_ob.get("state") == "pending")
        if _suppressed:
            _pepoch = (_ob or {}).get("stop_epoch")
            logger.warning(f"POST /tasks/{task_id}/result: [PAUSE-SUPPRESSED] cascade 억제됨 "
                           f"(task_type={task_type}, status={result.status}, outbox=pending, "
                           f"stop_epoch={_pepoch}). result는 저장됨, 후속 stage 미생성.")
            return {"status": "ok", "task_id": task_id, "suppressed_by_pause": True,
                    "pause_generation": _pepoch, "cascade_suppressed": True}
        if _artifact_held is not None:
            logger.warning("POST /tasks/%s/result: no artifact — %s", task_id, _artifact_held)
            return {"status": "ok", "task_id": task_id, "held": "no_artifact",
                    "reason": "no_artifact", "detail": _artifact_held}
        if task_type == "discuss":
            agent = ctx.get("agent") if isinstance(ctx, dict) else None
            logger.info(f"POST /tasks/{task_id}/result: discuss task, pushing next discuss for agent={agent}")
            _try_push_discuss(agent)
        else:
            # Failure handling: rate-limit → reroute via fallback chain (#81),
            # otherwise auto-retry the same role up to MAX_RETRIES.
            if result.status == "failed":
                logger.info(f"POST /tasks/{task_id}/result: task failed with status=failed, evaluating fallback/retry")
                if not _auto_fallback_failed_task(task_id, result, task_type):
                    _auto_retry_failed_task(task_id, result, task_type)
            # Auto-transition: impl task completed → auto-enqueue review task.
            # Pass through the PR number from the impl result so the reviewer
            # task description nails down which PR head to diff (#86).
            # Skip when coordinator_managed=True — `crew run` drives transitions itself
            # to avoid duplicate tasks and _wait() blocking on the wrong task_id.
            _task_ctx = ctx if isinstance(ctx, dict) else {}
            if task_type == "implement" and result.status == "completed":
                if _task_ctx.get("coordinator_managed"):
                    logger.info(f"POST /tasks/{task_id}/result: coordinator_managed — skipping auto review enqueue")
                else:
                    logger.info(f"POST /tasks/{task_id}/result: impl task completed, auto-enqueueing review")
                    _auto_enqueue_review(task_id, pr_number=result.pr_number,
                                         result=result)
            # Auto-transition: review approved → auto-enqueue test task. Use
            # the defensive verdict resolver so a clean `verdict=null`+`[]`
            # review counts as approved (#100). Skip when the review task was
            # created with no_tester=True (set by `crew run --no-tester`).
            # #178: post review verdict as GitHub PR comment
            # ⛔Bound for EVERY task type, not just reviews. The approve gate
            #   below reads it unconditionally, and assigning it only inside the
            #   review branch made every non-review result raise
            #   UnboundLocalError — 59 suites, caught by the full run.
            _pub = None
            if task_type == "review":
                _review_pr = result.pr_number or (ctx.get("pr_number") if isinstance(ctx, dict) else None)
                _review_ctx_repo = (ctx.get("repo") if isinstance(ctx, dict) else "") or ""
                _reviewer_wt = (_load_worktree_map(state_path) or {}).get("reviewer", "")
                _review_repo = _review_ctx_repo or (get_repo(cwd=_reviewer_wt) if _reviewer_wt else "") or ""
                # #304: a verdict describes the commit that was READ. If the PR
                # head has moved since this review was prepared, posting it
                # attributes a judgement to code the reviewer never saw — a
                # false statement about the PR, not merely a stale one. Measured
                # downstream 2026-09-14: a review pinned at 5b496a9f published
                # request_changes while the head was 28419e96, and the PR moved
                # on again with no head-anchored review anywhere.
                _pub = review_publication_decision(
                    ctx if isinstance(ctx, dict) else {}, _review_pr,
                    repo=_review_repo,
                    repo_cwd=_reviewer_wt,
                ) if _review_pr else None
                if _pub is not None and not _pub.publish:
                    logger.warning(
                        f"POST /tasks/{task_id}/result: NOT publishing this verdict to "
                        f"PR #{_review_pr} — {_pub.reason} (#304). The result itself is "
                        f"recorded; only the attribution to the wrong commit is stopped."
                    )
                    try:
                        q().patch_context(task_id, {
                            "review_publication": _pub.status,
                            "review_publication_reason": _pub.reason,
                        })
                    except Exception:  # noqa: BLE001 — telemetry never breaks a result
                        logger.exception(
                            f"POST /tasks/{task_id}/result: could not record the "
                            f"suppressed publication for {task_id}")
                    if _pub.requeue_head:
                        _requeue_review_at_head(task_id, _review_pr, _pub.requeue_head, ctx)
                    _review_pr = None
                if _review_pr and _REPLAYING.get():
                    # #314 §4 P0: replay 중 PR review comment 재게시 금지(gh comment는 receipt 없음
                    # → 게시 후 ACK 전 crash 시 중복). 라이브 경로에서만 게시. verdict 소비(test/merge)는
                    # 아래에서 계속되며 stable id/receipt로 멱등.
                    logger.debug(f"POST /tasks/{task_id}/result: replay 중 — review comment skip (PR #{_review_pr})")
                elif _review_pr:
                    # #314 재리뷰: review comment도 GitHub 외부 mutation이므로 merge와 동일한 **원자
                    # STOP admission**을 지난다. external_op_reserve가 같은 BEGIN IMMEDIATE에서
                    # runtime_stop을 확인해, STOP이 먼저 linearize됐으면 admitted=False로 차단(outbox
                    # applied 직후 STOP 걸린 comment race 제거). receipt(done)로 comment 성공→기록 전
                    # crash 재시도 시 중복도 방지. root cause가 GitHub comment feedback loop였으므로 필수.
                    _cop = f"comment:review:{task_id}"
                    _cresv = q().external_op_reserve(_cop, pr_number=int(_review_pr))
                    if not _cresv.get("admitted"):
                        logger.warning(f"POST /tasks/{task_id}/result: review comment 억제 — "
                                       f"STOP admission 거부(PR #{_review_pr}, {_cresv.get('state')})")
                    elif _cresv.get("state") == "done":
                        logger.info(f"POST /tasks/{task_id}/result: review comment 이미 done(receipt) — skip")
                    elif not _review_repo:
                        q().external_op_mark(
                            _cop, "failed",
                            last_error="repo identity unresolved (no ctx.repo, no reviewer worktree remote) "
                                       "— fail-closed, no external mutation attempted",
                            inc_attempt=True,
                        )
                        logger.warning(
                            f"POST /tasks/{task_id}/result: review comment 억제 — repo identity 미확정, "
                            f"fail-closed(PR #{_review_pr}, mutation 0)"
                        )
                    else:
                        # #314 재리뷰: review comment crash reconciliation. 게시 성공→done 기록 전
                        # crash면 DB엔 reserved만 남고 재진입 시 중복 게시될 수 있다. merge의 pr_state
                        # reconciliation과 동일하게, 기존 reserved(crash 의심)면 GitHub에 stable
                        # marker('task: {id}')가 이미 있는지 먼저 확인한다:
                        #   있음→재게시 없이 done / unknown→feedback fail-closed 미게시 / 없음→게시.
                        # 그리고 post_review_comment는 실패를 False로 반환하므로 True일 때만 done.
                        from agent_crew.github import post_review_comment, pr_has_comment_containing
                        _marker = f"task: {task_id}"
                        _do_post = True
                        if not _cresv.get("reserved"):   # 기존 reserved = crash 의심 → 먼저 재확인
                            _existing = pr_has_comment_containing(int(_review_pr), _marker, repo=_review_repo)
                            if _existing is True:
                                q().external_op_mark(_cop, "done")
                                logger.info(f"POST /tasks/{task_id}/result: review comment already on "
                                            f"PR #{_review_pr} (reconciled) → done, 재게시 안 함")
                                _do_post = False
                            elif _existing is None:
                                logger.warning(f"POST /tasks/{task_id}/result: review comment 존재 확인 "
                                               f"불가(unknown) → fail-closed 미게시(재확인 대기)")
                                _do_post = False
                        if _do_post:
                            _reviewer_agent = next(
                                (k for k in (pane_map or {}) if k in ("claude", "codex", "gemini")),
                                "agent",
                            )
                            _ok = post_review_comment(
                                pr_number=int(_review_pr),
                                verdict=_resolve_verdict(result),   # #208 defensive resolver
                                summary=result.summary or "",
                                findings=result.findings or [],
                                task_id=task_id,
                                reviewer=_reviewer_agent,
                                repo=_review_repo,
                            )
                            if _ok:
                                q().external_op_mark(_cop, "done")
                                logger.info(f"POST /tasks/{task_id}/result: posted review comment on PR #{_review_pr}")
                            else:
                                q().external_op_mark(_cop, "failed",
                                                     last_error="post_review_comment returned False",
                                                     inc_attempt=True)
                                logger.warning(f"POST /tasks/{task_id}/result: post_review_comment "
                                               f"False → done 미기록(재시도 가능, PR #{_review_pr})")

            # #304 review (P1): suppressing the COMMENT was not enough. The
            # verdict's CONSEQUENCES ran regardless — an approve with
            # `no_tester=True` reached `_auto_merge_pr`, otherwise it spent a
            # tester — so a stale approval of the old commit could merge a PR
            # whose head had moved. That is strictly worse than the
            # mis-attributed comment #304 set out to stop, because a merge
            # cannot be undone by a later head-anchored review.
            #
            # ⛔Every review-driven transition is gated on the same decision,
            #   not just the one that writes to GitHub.
            _verdict_is_about_this_head = _pub is None or _pub.publish
            if (task_type == "review" and _resolve_verdict(result) == "approve"
                    and not _verdict_is_about_this_head):
                logger.warning(
                    f"POST /tasks/{task_id}/result: approval NOT acted on — "
                    f"{_pub.reason} (#304). No tester enqueued and no merge: this "
                    f"verdict is about a different commit."
                )
            elif task_type == "review" and _resolve_verdict(result) == "approve":
                review_ctx = ctx if isinstance(ctx, dict) else {}
                pr_number = result.pr_number or review_ctx.get("pr_number")
                if review_ctx.get("no_tester"):
                    logger.info(f"POST /tasks/{task_id}/result: review approved but no_tester=True — skipping test enqueue")
                    # #171: no tester stage → merge immediately on review approval
                    if pr_number and not review_ctx.get("coordinator_managed"):
                        _auto_merge_pr(int(pr_number), repo=_review_repo, repo_cwd=_reviewer_wt)
                elif review_ctx.get("coordinator_managed"):
                    logger.info(f"POST /tasks/{task_id}/result: coordinator_managed — skipping auto test enqueue")
                else:
                    logger.info(f"POST /tasks/{task_id}/result: review task approved, auto-enqueueing test")
                    _auto_enqueue_test(task_id, repo=_review_repo)
            # #244: review requested changes → auto-enqueue the fix. The
            # rejection path used to just end here, so every extra review round
            # needed an operator to hand-enqueue the fix with the findings
            # pasted in. Bounded by AGENT_CREW_REVIEW_FIX_MAX_ROUNDS inside the
            # cascade, so a reviewer that keeps rejecting cannot spin the loop.
            elif (task_type == "review" and _resolve_verdict(result) == "request_changes"
                    and _review_result_is_actionable(result)):
                review_ctx = ctx if isinstance(ctx, dict) else {}
                if review_ctx.get("coordinator_managed"):
                    logger.info(f"POST /tasks/{task_id}/result: coordinator_managed — skipping auto fix enqueue")
                else:
                    logger.info(f"POST /tasks/{task_id}/result: review requested changes, auto-enqueueing fix")
                    _auto_enqueue_fix(task_id, repo=_review_repo)
            # #171: test passed → merge the PR. pr_number carried via test context.
            if task_type == "test" and result.status == "completed":
                if not _task_ctx.get("coordinator_managed"):
                    test_pr = result.pr_number or _task_ctx.get("pr_number")
                    if test_pr:
                        _test_wt = _any_worktree_path()
                        _test_repo = _task_ctx.get("repo") or ""
                        _auto_merge_pr(int(test_pr), repo=_test_repo, repo_cwd=_test_wt)
            # Task done → that role is now idle → push the next pending task of the same role.
            role = _TYPE_TO_ROLE.get(task_type)
            logger.info(f"POST /tasks/{task_id}/result: task_type={task_type} -> role={role}, calling _try_push_next")
            if role:
                _try_push_next(role)
        return {"status": "ok"}

    @app.post("/admin/replay-suppressed", status_code=200)
    def replay_suppressed():
        """#314 §4: 유효한 재개(unpaused) 후, STOP으로 억제됐던 result-cascade를 cascade_outbox에서
        drain해 1회 replay한다. lease CAS(outbox_claim)로 동시 replay를 dedup하고, 저장된 result_json
        으로 production submit_result를 재실행(cascade 중복 없음). 성공 시에만 applied로 CAS(at-most-once).
        crash한 replaying은 lease 만료 후 다음 호출/부팅에서 reclaim된다. fail-closed: 여전히 STOP이면 skip."""
        from agent_crew import pause as _pm
        import uuid as _uuid
        _sd = os.path.dirname(q()._db_path)
        # 여전히 STOP(runtime_stop 권위 OR pause.json additive)이면 replay 금지.
        try:
            if bool(q().get_stop_epoch()["paused"]) or _pm.is_paused(_sd):
                return {"status": "skipped", "reason": "still paused", "replayed": []}
        except Exception:
            return {"status": "skipped", "reason": "pause 판정불가(fail-closed)", "replayed": []}
        from agent_crew.protocol import TaskResult
        done = []
        for rec in q().outbox_pending(include_replaying=True):
            parent = rec.get("parent_task_id")
            if not parent:
                continue
            owner = f"replay-{_uuid.uuid4().hex[:8]}"
            claim = q().outbox_claim(parent, owner)      # lease CAS: pending/만료replaying만
            if not claim:
                continue                                 # 다른 executor 처리중 or 이미 applied
            # #314 §4 P0: replay 동안 _REPLAYING=True → submit_result가 durable cascade transition
            # (successor enqueue, stable id 멱등)만 재실행하고 non-idempotent side effect(PR comment/
            # escalation gate+telegram/queue push)는 skip. replay side-effect boundary 확립.
            _rtok = _REPLAYING.set(True)
            try:
                rd = json.loads(claim.get("result_json") or "{}")
                submit_result(parent, TaskResult(**rd))  # outbox 'replaying' → cascade transition만 재실행
                q().outbox_mark_applied(parent, owner)    # 성공분만 replaying→applied CAS
                done.append(parent)
            except Exception:
                logger.exception(f"replay-suppressed: {parent} 실패(lease 만료 후 reclaim)")
            finally:
                _REPLAYING.reset(_rtok)
        return {"status": "ok", "replayed": done,
                "pending_remaining": len(q().outbox_pending(include_replaying=False))}

    @app.delete("/tasks/{task_id}", status_code=200)
    def cancel_task(task_id: str):
        q().cancel(task_id)
        return {"status": "cancelled"}

    @app.post("/tasks/{task_id}/recover", status_code=200)
    def recover_orphan_task(task_id: str, force: bool = False):
        """**단건** orphan 을 pending 으로 되돌린다(취소가 아니라 재큐).

        ⭐전역 `expire-stale` 없이 한 건만 안전하게 회수하기 위한 경로다.
        ⛔dispatcher lease 가 살아 있으면 거부한다 — 돌고 있는 task 를 되돌리면
          같은 worker 에 두 번째 프로세스가 붙는다. `force=true` 로만 넘어간다.
        """
        _leased, _ = _dispatcher_lease_view()
        _status = q().get_task_status(task_id)
        if _status is None:
            raise HTTPException(status_code=404, detail=f"unknown task {task_id}")
        if _status != "in_progress":
            return {"task_id": task_id, "recovered": False,
                    "reason": f"status={_status} (in_progress 가 아님)"}
        if _leased is None and not force:
            return {"task_id": task_id, "recovered": False,
                    "reason": "dispatcher lease 를 못 읽었다 — lease_tracking=false. "
                              "살아 있는 task 를 되돌릴 수 있으므로 거부한다(force 로 강제)"}
        if _leased is not None and task_id in _leased and not force:
            return {"task_id": task_id, "recovered": False,
                    "reason": "dispatcher 가 이 task 의 lease 를 들고 있다(실행 중)"}
        q().requeue(task_id)
        logger.info(
            f"recover_orphan_task: {task_id} in_progress -> pending "
            f"(lease_tracking={_leased is not None}, force={force})"
        )
        return {"task_id": task_id, "recovered": True, "new_status": "pending",
                "lease_tracking": _leased is not None, "force": force}

    @app.post("/tasks/expire-stale", status_code=200)
    def expire_stale_tasks(older_than: float = 600.0):
        """Cancel in_progress tasks idle longer than ``older_than`` seconds.
        Returns list of cancelled task_ids."""
        cancelled = q().expire_stale(older_than_seconds=older_than)
        return {"cancelled": cancelled}

    @app.post("/gates", status_code=201)
    def post_gate(gate: GateRequest):
        gate_id = q().create_gate(gate)
        return {"gate_id": gate_id}

    @app.get("/gates/pending")
    def get_pending_gates():
        return q().list_gates(status="pending")

    @app.get("/gates/{gate_id}")
    def get_gate(gate_id: str):
        gates = q().list_gates()
        for g in gates:
            if g.id == gate_id:
                return g
        raise HTTPException(status_code=404, detail=f"Gate {gate_id!r} not found")

    @app.post("/gates/{gate_id}/resolve", status_code=200)
    def resolve_gate(gate_id: str, body: ResolveBody):
        try:
            q().resolve_gate(gate_id, approved=body.status == "approved")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if body.status == "approved":
            # A Tier 3 gate is intentionally created before its successor so
            # generic pending-task pushing alone cannot advance it.  Resume
            # the exact held transition first; the helper is idempotent via
            # deterministic child IDs and recognizes review vs test gates.
            try:
                _resume_tier3_gate(q(), gate_id)
            except Exception:
                logger.exception(
                    "resolve_gate: failed to resume Tier 3 gate %r after approval", gate_id
                )
            # Gate approved → push next pending tasks for all roles so the crew
            # continues without manual intervention after a human approval.
            for role in ("implementer", "reviewer", "tester"):
                try:
                    _try_push_next(role)
                except Exception:
                    logger.exception(
                        f"resolve_gate: _try_push_next({role!r}) raised after gate approval"
                    )
        return {"status": "resolved"}

    @app.post("/tasks/{task_id}/checkpoint", status_code=201)
    def save_checkpoint(task_id: str, checkpoint: dict):
        """Save a task checkpoint for fault recovery and time-travel debugging."""
        checkpoint_num = checkpoint.get("checkpoint_num", 0)
        state = checkpoint.get("state", {})
        try:
            checkpoint_id = q().save_checkpoint(task_id, checkpoint_num, state)
            logger.info(f"POST /tasks/{task_id}/checkpoint: saved checkpoint {checkpoint_num}")
            return {"checkpoint_id": checkpoint_id}
        except Exception as e:
            logger.error(f"POST /tasks/{task_id}/checkpoint: error: {e}")
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/tasks/{task_id}/checkpoints")
    def list_task_checkpoints(task_id: str):
        """List all checkpoints for a task."""
        try:
            checkpoints = q().list_checkpoints(task_id)
            logger.info(f"GET /tasks/{task_id}/checkpoints: found {len(checkpoints)} checkpoints")
            return checkpoints
        except Exception as e:
            logger.error(f"GET /tasks/{task_id}/checkpoints: error: {e}")
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/tasks/{task_id}/checkpoint/{checkpoint_num}")
    def get_task_checkpoint(task_id: str, checkpoint_num: int):
        """Retrieve a specific checkpoint for time-travel debugging."""
        try:
            state = q().get_checkpoint(task_id, checkpoint_num)
            if state is None:
                raise HTTPException(status_code=404, detail=f"Checkpoint {checkpoint_num} not found for task {task_id}")
            logger.info(f"GET /tasks/{task_id}/checkpoint/{checkpoint_num}: retrieved")
            return {"checkpoint_num": checkpoint_num, "state": state}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"GET /tasks/{task_id}/checkpoint/{checkpoint_num}: error: {e}")
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/tasks/{task_id}/checkpoint/latest")
    def get_latest_task_checkpoint(task_id: str):
        """Retrieve the latest checkpoint for a task."""
        try:
            result = q().get_latest_checkpoint(task_id)
            if result is None:
                raise HTTPException(status_code=404, detail=f"No checkpoints found for task {task_id}")
            checkpoint_num, state = result
            logger.info(f"GET /tasks/{task_id}/checkpoint/latest: checkpoint {checkpoint_num}")
            return {"checkpoint_num": checkpoint_num, "state": state}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"GET /tasks/{task_id}/checkpoint/latest: error: {e}")
            raise HTTPException(status_code=400, detail=str(e))

    return app


def _load_pane_map() -> Optional[dict]:
    path = os.getenv("AGENT_CREW_PANE_MAP")
    logger.info(f"_load_pane_map: AGENT_CREW_PANE_MAP={path}")
    if not path:
        logger.warning("_load_pane_map: AGENT_CREW_PANE_MAP not set")
        return None
    path = os.path.expanduser(path)  # Handle ~ in env var
    logger.info(f"_load_pane_map: expanded path={path}")
    try:
        with open(path) as f:
            pane_map = json.load(f)
            logger.info(f"_load_pane_map: loaded pane_map={pane_map}")
            return pane_map
    except FileNotFoundError:
        logger.error(f"_load_pane_map: file not found: {path}")
        return None


app = create_app(
    db_path=os.path.expanduser(os.getenv("AGENT_CREW_DB", "~/.agent_crew/default.db")),
    pane_map=_load_pane_map(),
    port=int(os.getenv("AGENT_CREW_PORT", "0") or 0),
    state_path=os.path.expanduser(os.getenv("AGENT_CREW_STATE", "")) or None,
)
