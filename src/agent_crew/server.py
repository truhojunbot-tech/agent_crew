import asyncio
import contextlib
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
from agent_crew.anomaly import check_wrong_repo
from agent_crew import context_pack as _cpack
from agent_crew.context_identity import (
    append_attribution_jsonl,
    detect_context_compaction,
    extract_claude_session_id,
    record_context_event,
)
from agent_crew.fallback import is_rate_limit_error
from agent_crew.loop import _resolve_verdict
from agent_crew.pipeline import (
    auto_enqueue_fix as _pipeline_auto_enqueue_fix,
    auto_enqueue_review as _pipeline_auto_enqueue_review,
    auto_enqueue_test as _pipeline_auto_enqueue_test,
    auto_fallback_failed_task as _pipeline_auto_fallback_failed_task,
)
from agent_crew import provenance as _prov
from agent_crew.protocol import GateRequest, TaskRequest, TaskResult
from agent_crew.queue import TaskQueue, _ROLE_TO_TYPE, _TYPE_TO_ROLE

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


def _resolve_pr_head_branch(pr_number: int) -> Optional[str]:
    """Return the head ref name for a GitHub PR, or None on failure."""
    try:
        r = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "headRefName",
             "-q", ".headRefName"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            branch = r.stdout.strip()
            if branch:
                return branch
    except Exception:
        pass
    return None


def _prepare_worktree_for_task(
    worktree_path: str,
    task_id: str,
    task_branch: str,
    role: str,
    task_context: Optional[dict] = None,
) -> None:
    """Sync worktree to origin and checkout the right branch before task dispatch.

    - All roles: stash local changes, fetch origin (catches stale worktrees that
      missed weeks of merged PRs, #141).
    - implementer: checkout a fresh branch per task from origin/main so each
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
        _prepare_worktree_for_task_inner(worktree_path, task_id, task_branch, role,
                                         task_context=task_context or {})
    except Exception:
        logger.exception(
            f"_prepare_worktree_for_task: unexpected error for {role} "
            f"task_id={task_id} — continuing"
        )


def _prepare_worktree_for_task_inner(
    worktree_path: str,
    task_id: str,
    task_branch: str,
    role: str,
    task_context: Optional[dict] = None,
) -> None:
    """Inner (may raise). Wrapped by _prepare_worktree_for_task."""
    main_branch = _WORKTREE_MAIN_BRANCH
    if task_context is None:
        task_context = {}
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
        # Fresh branch per task from origin/main (#140). Use task.branch when
        # set (crew run --branch), otherwise derive from task_id.
        branch = task_branch if task_branch else f"agent/{task_id[:12]}"
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
        prefix = "review" if role == "reviewer" else "test"
        local_branch = f"{prefix}/{task_id[:8]}"

        pr_branch = task_branch  # fallback: base branch from task.branch
        pr_number = task_context.get("pr_number")
        if pr_number:
            resolved = _resolve_pr_head_branch(int(pr_number))
            if resolved:
                pr_branch = resolved
                logger.info(
                    f"_prepare_worktree_for_task: resolved PR #{pr_number} "
                    f"head → {pr_branch!r} for {role} {task_id}"
                )
            else:
                logger.warning(
                    f"_prepare_worktree_for_task: could not resolve PR #{pr_number} "
                    f"head for {role} {task_id} — falling back to task.branch={task_branch!r}"
                )

        target_ref = f"origin/{pr_branch}" if pr_branch else f"origin/{main_branch}"
        r = subprocess.run(
            ["git", "-C", worktree_path, "checkout", "-B", local_branch, target_ref],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            logger.warning(
                f"_prepare_worktree_for_task: {role} checkout {local_branch} "
                f"from {target_ref} failed, falling back to origin/{main_branch}: "
                f"{r.stderr.strip()}"
            )
            subprocess.run(
                ["git", "-C", worktree_path, "checkout", "-B", local_branch,
                 f"origin/{main_branch}"],
                capture_output=True, text=True, timeout=30,
            )


_DEFAULT_ROLE_TO_AGENT = {"implementer": "claude", "reviewer": "codex", "tester": "gemini"}
_DEFAULT_AGENT_TO_ROLE = {v: k for k, v in _DEFAULT_ROLE_TO_AGENT.items()}


# Tags that mean "retryable in the next few minutes" (server-side load-shed).
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
        # New schema: explicit roles list with per-role worktree.
        roles_list = state.get("roles")
        if roles_list:
            result = {
                r["role"]: r["worktree"]
                for r in roles_list
                if r.get("role") and r.get("worktree")
            }
            logger.info(f"_load_worktree_map: state_path={state_path!r} (roles) → worktree_map={result}")
            return result
        # Legacy schema: agent-keyed worktrees + default role mapping.
        worktrees = state.get("worktrees", {})
        result = {}
        for agent, path in worktrees.items():
            role = _DEFAULT_AGENT_TO_ROLE.get(agent)
            if role and path:
                result[role] = path
        logger.info(f"_load_worktree_map: state_path={state_path!r} (legacy) → worktree_map={result}")
        return result
    except Exception:
        logger.exception("_load_worktree_map: failed to read state.json")
        return {}


def _load_role_to_agent(state_path: Optional[str]) -> dict[str, str]:
    """Load {role: agent_name} mapping from state.json's roles list.

    Falls back to the hardcoded default when state.json lacks a roles list
    (legacy setups). Always returns all 3 roles populated — missing roles
    default to the legacy assignment.
    """
    result = dict(_DEFAULT_ROLE_TO_AGENT)
    if not state_path or not os.path.exists(state_path):
        return result
    try:
        with open(state_path) as f:
            state = json.load(f)
        for r in state.get("roles") or []:
            role = r.get("role")
            agent = r.get("agent")
            if role and agent:
                result[role] = agent
    except Exception:
        logger.exception("_load_role_to_agent: failed to read state.json")
    return result


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


def _pane_token_count(pane_id: str) -> int:
    """Return the token count hinted in the pane status bar, or 0.

    Claude Code shows "new task? /clear to save 544.1k tokens" when context
    is large. We parse that hint to detect saturation (#133).
    """
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane_id],
            capture_output=True, text=True,
        )
    except Exception:
        return 0
    m = _TOKEN_COUNT_RE.search(r.stdout)
    if not m:
        return 0
    raw = m.group(1).replace(",", "")
    if raw.lower().endswith("k"):
        return int(float(raw[:-1]) * 1_000)
    if raw.lower().endswith("m"):
        return int(float(raw[:-1]) * 1_000_000)
    return int(float(raw))


def _pane_clear_context(pane_id: str) -> None:
    """Send /clear to a Claude pane to reset context (#133)."""
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "/clear", "Enter"],
                   capture_output=True)
    import time as _time
    _time.sleep(2.0)


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
    """
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

    def q() -> TaskQueue:
        return state["queue"]

    # Expose watchdog tick on app.state so tests can drive it deterministically
    # without the asyncio loop. Production code never reads this attribute.
    app.state.reminded_task_ids = reminded_task_ids

    def _try_push_next(role: str) -> None:
        """If the role has an available pane and is idle, dequeue and push the next task."""
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
        if q().has_in_progress(task_type):
            logger.debug(f"_try_push_next: task_type {task_type} already in progress")
            return  # agent busy; will get pushed when current task completes
        task = q().dequeue(role=role)
        if task is None:
            logger.debug(f"_try_push_next: no pending task for role {role}")
            return  # nothing pending

        # Check if task has an agent_override in context
        task_context = task.context if isinstance(task.context, dict) else {}
        logger.debug(f"_try_push_next: task_id={task.task_id}, context={task_context}")
        if "agent_override" in task_context:
            agent_override = task_context["agent_override"]
            override_pane_id = pane_map.get(agent_override)
            if override_pane_id:
                logger.info(f"_try_push_next: using agent override {agent_override} (pane {override_pane_id}) instead of role {role}")
                pane_id = override_pane_id
            else:
                logger.warning(f"_try_push_next: agent_override {agent_override} not found in pane_map")
                return

        # Verify pane is alive before pushing — dead pane causes silent task loss.
        if not _pane_alive_for_push(pane_id):
            logger.error(
                f"_try_push_next: pane {pane_id} is dead — rolling task "
                f"{task.task_id} back to queued"
            )
            q().requeue(task.task_id)
            return

        # #140/#141: prepare worktree branch before task delivery.
        if worktree_map and not _WORKTREE_SYNC_DISABLED:
            wt_path = worktree_map.get(role)
            if wt_path:
                try:
                    _prepare_worktree_for_task(
                        wt_path, task.task_id, task.branch or "", role,
                        task_context=task.context if isinstance(task.context, dict) else {},
                    )
                    logger.info(
                        f"_try_push_next: worktree prepared for {role} "
                        f"task_id={task.task_id} branch={task.branch or '(none)'}"
                    )
                except Exception:
                    logger.exception(
                        f"_try_push_next: worktree prep failed for {role} "
                        f"task_id={task.task_id} — continuing with dispatch"
                    )

        # #151: if target pane shows a usage-limit message, immediately reroute
        # via fallback rather than pushing into a blocked agent.
        if _pane_has_usage_limit(pane_id):
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
        if _pane_has_bash_prompt(pane_id):
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
        tok = _pane_token_count(pane_id)
        if _push_enabled and tok >= _TOKEN_CLEAR_THRESHOLD:
            logger.info(
                f"_try_push_next: pane {pane_id} has {tok} tokens "
                f"(>= {_TOKEN_CLEAR_THRESHOLD}) — sending /clear before push"
            )
            _pane_clear_context(pane_id)
        # #134: auto-dismiss gemini permission prompt if present.
        _pane_dismiss_permission_prompt(pane_id)
        push_fn(pane_id, _format_task_message(task, port))
        # #152: record the moment the task was actually pushed to the pane
        # so the watchdog measures idle_for from push time, not dequeue time.
        q().set_push_at(task.task_id)

    def _try_push_discuss(agent: Optional[str]) -> None:
        """Discuss tasks fan out per agent, not per role. pane_map is expected
        to hold agent-name keys (e.g. 'claude', 'codex', 'gemini') alongside
        the role keys. Busy-check and dequeue are both scoped to the agent so
        concurrent panelists don't block each other."""
        logger.debug(f"_try_push_discuss: agent={agent}")
        if not _push_enabled:
            logger.warning(
                f"_try_push_discuss: AGENT_CREW_DELIVERY={_delivery_raw!r} — tmux push disabled. "
                "Discuss tasks will only be delivered if an MCP client polls GET /tasks/next."
            )
            return
        if not pane_map or not agent:
            logger.debug(f"_try_push_discuss: no pane_map or agent")
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
        # Verify pane is alive before pushing — dead pane causes silent task loss.
        if not _pane_alive_for_push(pane_id):
            logger.error(
                f"_try_push_discuss: pane {pane_id} is dead — rolling discuss task "
                f"{task.task_id} back to queued"
            )
            q().requeue(task.task_id)
            return

        logger.info(f"_try_push_discuss: dequeued task_id={task.task_id} for agent={agent}, calling push_fn")
        push_fn(pane_id, _format_task_message(task, port))
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

    def _fail_if_active(task_id: str, reason: str) -> None:
        """Fail a task only when it is still in_progress (agent may have submitted first)."""
        tasks = q().list_tasks(status="in_progress")
        if any(t.task_id == task_id for t in tasks):
            try:
                q().submit_result(
                    task_id,
                    TaskResult(task_id=task_id, status="failed", summary=reason,
                               error_info={"reason": reason}),
                )
                _attr = q().get_attribution(task_id)
                record_context_event(
                    _context_events_path, "task_failed",
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
        # ⛔Initialised before the gemini branch so the event gate below can
        #   read it for ANY agent. This boolean is the cap decision itself —
        #   `_force_context_reset` is not, because an operator's explicit
        #   task.context.context_reset sets it too (review-99ad8ad0).
        _agy_over = False
        _agy_cap_info = {}
        if agent == "gemini":
            _agy_over, _agy_cap_info = agy_context_exceeds_cap(wt)
            if _agy_over:
                _force_context_reset = True
                logger.warning(
                    "dispatcher: agy context %s for %s is %.1f MB (cap %.0f MB) — "
                    "forcing a fresh provider conversation (#236)",
                    _agy_cap_info.get("conversation_id", "?"), wt,
                    _agy_cap_info.get("bytes", 0) / 1048576.0,
                    _agy_cap_info.get("cap_mb", 0),
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
            if _agy_over:
                record_context_event(
                    _context_events_path, "provider_context_capped",
                    task_id=task.task_id, project=_project, role=role, agent=agent,
                    context_id=_ctx_info["context_id"],
                    context_generation=_ctx_info["context_generation"],
                    provider="agy",
                    conversation_id=_agy_cap_info.get("conversation_id", ""),
                    bytes=_agy_cap_info.get("bytes", 0),
                    cap_mb=_agy_cap_info.get("cap_mb", 0),
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
                provider_session_id=_ctx_info.get("provider_session_id") or "",
                context_policy=_ctx_info["context_policy"],
                context_generation=_ctx_info["context_generation"],
                session_task_index=_ctx_info["session_task_index"],
                previous_task_id=_ctx_info.get("previous_task_id") or "",
                retry_of=_retry_of,
                fallback_of=_fallback_of,
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

        # Prepare worktree: stash local changes, fetch origin, checkout right branch.
        if not _WORKTREE_SYNC_DISABLED:
            try:
                _prepare_worktree_for_task(
                    wt, task.task_id, task.branch or "", role,
                    task_context=task.context if isinstance(task.context, dict) else {},
                )
                logger.info(
                    f"dispatcher: worktree prepared for {role} "
                    f"task_id={task.task_id} branch={task.branch or '(none)'}"
                )
            except Exception:
                logger.exception(
                    f"dispatcher: worktree prep failed for {role} task_id={task.task_id} — continuing"
                )

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
                record_context_event(
                    _context_events_path, "context_pack_built",
                    task_id=task.task_id, project=_project, role=role, agent=agent,
                    context_id=_ctx_info["context_id"],
                    context_generation=_ctx_info["context_generation"],
                    **_pack.telemetry(),
                )
                # Durable linkage: the pack that produced this dispatch is
                # recorded on the task, so a terminal outcome can be attributed
                # back to the exact context it was given.
                q().patch_context(task.task_id, {
                    "context_pack_id": _pack.pack_id,
                    "context_pack_hash": _pack.pack_hash,
                    "context_pack_degraded": _pack.degraded,
                })
            except Exception:
                logger.exception(
                    f"dispatcher: context pack telemetry failed for {task.task_id}")
        # Per-role log file so `tail -f dispatch_{role}.log` in the pane
        # shows a continuous stream across all tasks for that role.
        log_path = os.path.join(os.path.dirname(db_path), f"dispatch_{role}.log")

        if agent == "claude":
            cmd = [
                "claude", "-p", message,
                "--continue", "--dangerously-skip-permissions",
                "--verbose", "--output-format", "stream-json",
            ]
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
            cmd += ["--dangerously-skip-permissions", "--model", _known_model]
        else:  # codex — resume last session for context continuity; falls back to fresh if none exists
            cmd = [
                "codex", "exec", "resume", "--last",
                "--dangerously-bypass-approvals-and-sandbox",
                message,
            ]

        timeout_secs = _dispatch_timeout_for_role(role)
        logger.info(f"dispatcher: {agent} task={task.task_id} role={role} wt={wt} timeout={timeout_secs}s")
        # Only pop the retry counter on a terminal outcome. Flipped to False
        # right before the early `return` on a successful requeue — that
        # `return` still runs `finally`, so without this flag the counter
        # was erased every attempt and _MAX_TRANSIENT_RETRY never actually
        # capped anything (#201).
        _terminal = True
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
                if detect_context_compaction(_task_log_tail):
                    record_context_event(
                        _context_events_path, "context_compacted",
                        task_id=task.task_id, project=_project, role=role, agent=agent,
                        context_id=_ctx_info["context_id"],
                    )
            except Exception:
                logger.exception(f"dispatcher: context observation failed for task={task.task_id}")
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
                _fail_if_active(task.task_id, "dispatcher_timeout")
            elif proc.returncode != 0:
                _fail_if_active(task.task_id, f"exit_{proc.returncode}")
            else:
                _fail_if_active(task.task_id, "no_result_submitted")
        except Exception:
            logger.exception(f"dispatcher: error task={task.task_id}")
            _fail_if_active(task.task_id, "dispatcher_exception")
        finally:
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
        """Poll DB every AGENT_CREW_DISPATCH_INTERVAL seconds and spawn headless
        agent subprocesses.  One concurrent task per role (same --continue session
        cannot be shared across parallel invocations) — AND, since #202
        review of PR #203 (finding 1), one concurrent task per *resolved*
        worktree, since agent_override can route two different roles into
        the same underlying agent/worktree/provider conversation. Role-slot
        exclusivity alone doesn't see that — e.g. tester's normal gemini
        task and a reviewer task with agent_override=gemini both occupy
        different role slots but resolve to the same gemini worktree, and
        running both `--continue` processes concurrently there would
        corrupt that conversation. See _resolve_dispatch_target.
        """
        active_roles: set[str] = set()
        active_worktrees: set[str] = set()
        active_tasks: dict[str, asyncio.Task] = {}  # task_id → asyncio.Task
        task_roles: dict[str, str] = {}  # task_id → role

        # Inverse of _DISPATCH_ROLE_TO_AGENT: agent → role (for worktree lookup).
        # When the same agent maps to multiple roles (e.g. claude on both
        # implementer and reviewer), prefer implementer for discuss dispatch
        # since discuss tasks are exploratory rather than review-specific.
        _AGENT_TO_ROLE: dict[str, str] = {}
        _ROLE_PRIORITY = {"implementer": 0, "reviewer": 1, "tester": 2}
        for _role, _agent in _DISPATCH_ROLE_TO_AGENT.items():
            current = _AGENT_TO_ROLE.get(_agent)
            if current is None or _ROLE_PRIORITY.get(_role, 99) < _ROLE_PRIORITY.get(current, 99):
                _AGENT_TO_ROLE[_agent] = _role

        interval = float(os.getenv("AGENT_CREW_DISPATCH_INTERVAL", "2"))
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    logger.debug(f"dispatcher: loop tick worktree_map_keys={list(worktree_map.keys())} active_roles={active_roles}")
                    # Reap completed dispatches, freeing their slots.
                    done = [tid for tid, t in list(active_tasks.items()) if t.done()]
                    for tid in done:
                        active_tasks.pop(tid, None)
                        role = task_roles.pop(tid, None)
                        if role:
                            active_roles.discard(role)

                    for role in ("implementer", "reviewer", "tester"):
                        if role in active_roles:
                            continue
                        task = q().dequeue(role=role)
                        if task is None:
                            continue
                        _target_agent, _target_wt = _resolve_dispatch_target(task, role)
                        if _target_wt and _target_wt in active_worktrees:
                            # Same worktree/provider conversation already in
                            # flight under a different role slot — put the
                            # task back and try again next tick, rather than
                            # running two `--continue` processes against one
                            # conversation concurrently.
                            logger.info(
                                f"dispatcher: deferring task={task.task_id} role={role} "
                                f"agent={_target_agent} — target worktree {_target_wt} "
                                "already active under another role this tick"
                            )
                            q().requeue(task.task_id)
                            continue
                        active_roles.add(role)
                        if _target_wt:
                            active_worktrees.add(_target_wt)
                        task_roles[task.task_id] = role

                        async def _run(t: TaskRequest = task, r: str = role, w: Optional[str] = _target_wt) -> None:
                            try:
                                await _dispatch_task(t, r)
                            finally:
                                active_roles.discard(r)
                                if w:
                                    active_worktrees.discard(w)
                                active_tasks.pop(t.task_id, None)
                                task_roles.pop(t.task_id, None)

                        active_tasks[task.task_id] = asyncio.create_task(_run())

                    # Discuss tasks are per-agent (not per-role); dispatch concurrently.
                    for agent in ("claude", "codex", "gemini"):
                        slot_key = f"discuss_{agent}"
                        if slot_key in active_roles:
                            continue
                        task = q().dequeue_discuss_for_agent(agent)
                        if task is None:
                            continue
                        role = _AGENT_TO_ROLE.get(agent, "implementer")
                        _target_agent, _target_wt = _resolve_dispatch_target(task, role)
                        if _target_wt and _target_wt in active_worktrees:
                            logger.info(
                                f"dispatcher: deferring discuss task={task.task_id} agent={agent} "
                                f"— target worktree {_target_wt} already active this tick"
                            )
                            q().requeue(task.task_id)
                            continue
                        active_roles.add(slot_key)
                        if _target_wt:
                            active_worktrees.add(_target_wt)
                        task_roles[task.task_id] = slot_key

                        async def _run_discuss(
                            t: TaskRequest = task, r: str = role, s: str = slot_key,
                            w: Optional[str] = _target_wt,
                        ) -> None:
                            try:
                                await _dispatch_task(t, r)
                            finally:
                                active_roles.discard(s)
                                if w:
                                    active_worktrees.discard(w)
                                active_tasks.pop(t.task_id, None)
                                task_roles.pop(t.task_id, None)

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
    app.state.dispatch_task = _dispatch_task
    # ── End headless dispatcher ───────────────────────────────────────────────

    def _auto_enqueue_review(
        impl_task_id: str,
        pr_number: Optional[int] = None,
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
        )
        if review_id:
            _try_push_next("reviewer")

    def _auto_enqueue_test(review_task_id: str) -> None:
        """HTTP-side wrapper: run the transport-agnostic cascade then push.
        See ``_auto_enqueue_review`` for the rationale (#123)."""
        test_id = _pipeline_auto_enqueue_test(
            q(),
            review_task_id,
            pane_map=pane_map,
        )
        if test_id:
            _try_push_next("tester")

    def _auto_enqueue_fix(review_task_id: str) -> None:
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
            )
            if fix_id:
                _try_push_next("implementer")
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
            retry_context["retry_attempt"] = db_retry_attempt + 1
            retry_context["original_task_id"] = task_id

            retry_req = TaskRequest(
                task_id=f"retry-{task_id}-{uuid.uuid4().hex[:4]}",
                task_type=task_type,  # type: ignore
                description=original_task.description,
                branch=original_task.branch,
                priority=original_task.priority + 1,  # Bump priority for retries
                context=retry_context,
            )
            q().enqueue(retry_req)
            logger.info(f"Task {task_id} auto-retried (attempt {result.retry_count + 1}/{MAX_RETRIES})")
            # Try to push the retry task
            role = _TYPE_TO_ROLE.get(task_type)
            if role:
                _try_push_next(role)
        except Exception as e:
            logger.warning(f"Failed to auto-retry task {task_id}: {e}")
            pass

    def _auto_merge_pr(pr_number: int) -> None:
        """Merge PR via gh CLI after the pipeline approves it (#171).

        Failures are logged and swallowed — a merge error must never break
        the result-submission response.
        """
        from agent_crew.github import get_repo, merge_pr
        repo = get_repo()
        ok = merge_pr(pr_number, merge_method="squash", repo=repo)
        if ok:
            logger.info(f"_auto_merge_pr: merged PR #{pr_number} (squash) — #171")
        else:
            logger.warning(
                f"_auto_merge_pr: gh pr merge #{pr_number} failed or gh not available — #171"
            )

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
        return {
            "status": "ok",
            "project": ident["project"],
            "identity": ident,
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
        logger.info(f"POST /tasks: task_type={task.task_type}, task_id (will assign)...")
        task_id = q().enqueue(task)
        logger.info(f"POST /tasks: enqueued task_id={task_id}")
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
        return {"task_id": task_id}

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
        try:
            task_type = q().submit_result(task_id, result)
            logger.info(f"POST /tasks/{task_id}/result: marked done, task_type={task_type}")
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
                    _auto_enqueue_review(task_id, pr_number=result.pr_number)
            # Auto-transition: review approved → auto-enqueue test task. Use
            # the defensive verdict resolver so a clean `verdict=null`+`[]`
            # review counts as approved (#100). Skip when the review task was
            # created with no_tester=True (set by `crew run --no-tester`).
            # #178: post review verdict as GitHub PR comment
            if task_type == "review":
                _review_pr = result.pr_number or (ctx.get("pr_number") if isinstance(ctx, dict) else None)
                if _review_pr:
                    try:
                        from agent_crew.github import post_review_comment
                        _reviewer_agent = next(
                            (k for k in (pane_map or {}) if k in ("claude", "codex", "gemini")),
                            "agent",
                        )
                        post_review_comment(
                            pr_number=int(_review_pr),
                            # #208: use the same defensive resolver as the
                            # auto-enqueue-test decision below, so a clean
                            # verdict=null+[] review (or a reviewer that only
                            # states "approve" in prose) doesn't render as a
                            # request_changes header while the summary says
                            # approve.
                            verdict=_resolve_verdict(result),
                            summary=result.summary or "",
                            findings=result.findings or [],
                            task_id=task_id,
                            reviewer=_reviewer_agent,
                        )
                        logger.info(f"POST /tasks/{task_id}/result: posted review comment on PR #{_review_pr}")
                    except Exception:
                        logger.exception(f"POST /tasks/{task_id}/result: failed to post review comment on PR #{_review_pr}")

            if task_type == "review" and _resolve_verdict(result) == "approve":
                review_ctx = ctx if isinstance(ctx, dict) else {}
                pr_number = result.pr_number or review_ctx.get("pr_number")
                if review_ctx.get("no_tester"):
                    logger.info(f"POST /tasks/{task_id}/result: review approved but no_tester=True — skipping test enqueue")
                    # #171: no tester stage → merge immediately on review approval
                    if pr_number and not review_ctx.get("coordinator_managed"):
                        _auto_merge_pr(int(pr_number))
                elif review_ctx.get("coordinator_managed"):
                    logger.info(f"POST /tasks/{task_id}/result: coordinator_managed — skipping auto test enqueue")
                else:
                    logger.info(f"POST /tasks/{task_id}/result: review task approved, auto-enqueueing test")
                    _auto_enqueue_test(task_id)
            # #244: review requested changes → auto-enqueue the fix. The
            # rejection path used to just end here, so every extra review round
            # needed an operator to hand-enqueue the fix with the findings
            # pasted in. Bounded by AGENT_CREW_REVIEW_FIX_MAX_ROUNDS inside the
            # cascade, so a reviewer that keeps rejecting cannot spin the loop.
            elif task_type == "review" and _resolve_verdict(result) == "request_changes":
                review_ctx = ctx if isinstance(ctx, dict) else {}
                if review_ctx.get("coordinator_managed"):
                    logger.info(f"POST /tasks/{task_id}/result: coordinator_managed — skipping auto fix enqueue")
                else:
                    logger.info(f"POST /tasks/{task_id}/result: review requested changes, auto-enqueueing fix")
                    _auto_enqueue_fix(task_id)
            # #171: test passed → merge the PR. pr_number carried via test context.
            if task_type == "test" and result.status == "completed":
                if not _task_ctx.get("coordinator_managed"):
                    test_pr = result.pr_number or _task_ctx.get("pr_number")
                    if test_pr:
                        _auto_merge_pr(int(test_pr))
            # Task done → that role is now idle → push the next pending task of the same role.
            role = _TYPE_TO_ROLE.get(task_type)
            logger.info(f"POST /tasks/{task_id}/result: task_type={task_type} -> role={role}, calling _try_push_next")
            if role:
                _try_push_next(role)
        return {"status": "ok"}

    @app.delete("/tasks/{task_id}", status_code=200)
    def cancel_task(task_id: str):
        q().cancel(task_id)
        return {"status": "cancelled"}

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
