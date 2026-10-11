"""``claude_cloud``: a provider-neutral Cloud execution backend (#496).

Dispatches an eligible task via the Claude Code CLI's ``--cloud`` surface
instead of a local tmux pane, and reuses the existing Agent Crew lifecycle
rather than building a parallel one:

- ``TaskQueue.dequeue`` / ``record_dispatch`` — the SAME STOP/pause and CEA
  admission gates a tmux dispatch goes through (queue.py).
- ``TaskQueue.record_attribution`` / ``patch_context`` — the SAME task
  DB fields used to persist a provider's session id and lineage.
- ``pipeline.auto_enqueue_review`` / ``auto_fallback_failed_task`` — the SAME
  review cascade and rate-limit fallback local providers use.
- ``github.py`` — the SAME PR discovery primitives (branch → PR number,
  PR head SHA), extended with two small read-only additions
  (``pr_number_for_branch``, ``branch_head_commit_message``) since a cloud
  session has no local callback channel to this dispatcher.

This module adds no new orchestrator, scheduler, review pipeline, quota
system, or deploy path. It does not merge or deploy anything.

Genuinely missing seams (documented, not invented — see the PR body):

- No headless status read exists for a `claude --cloud` session id on the
  installed CLI (2.1.282) — completion is observed only via GitHub (a PR, or
  a branch's HEAD commit message), never by the session calling back in.
- Wiring ``reconcile_cloud_dispatch``/``reconcile_all_cloud_tasks`` into a
  periodic trigger (cron, watchdog tick, or a new ``crew`` subcommand) is an
  operator/coordinator integration decision left for a follow-up, not
  invented here as a new scheduler.
- The exact CLI resume syntax for "send findings back to the same cloud
  session" is inferred from the one observed launch (``claude --cloud
  <session_id> "<prompt>"``), gated behind ``probe_cloud_cli`` and a
  fail-closed parse — never assumed to work silently.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from agent_crew import github as _github
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import AdmissionRefused, TaskQueue

logger = logging.getLogger(__name__)

#: Fixed backend/provider identity. An operator opts a role into this
#: backend by setting ``role_agents.<role> = "claude_cloud"`` — role_mapping.py
#: is already provider-string-agnostic, so this needs no changes there.
CLOUD_PROVIDER_NAME = "claude_cloud"
#: `TaskQueue.record_dispatch(channel=...)` vocabulary (queue.py: "tmux_pane,
#: claude_p, codex_exec, gemini_cli, api"). This adds one value.
DISPATCH_CHANNEL = "claude_cloud"

_ENV_ENABLED = "AGENT_CREW_CLOUD_ENABLED"
_ENV_MAX_CONCURRENCY = "AGENT_CREW_CLOUD_MAX_CONCURRENCY"
_ENV_MAX_PER_DAY = "AGENT_CREW_CLOUD_MAX_PER_DAY"
_ENV_SHADOW = "AGENT_CREW_CLOUD_SHADOW"
_ENV_CLI_PATH = "AGENT_CREW_CLOUD_CLI_PATH"
DEFAULT_MAX_CONCURRENCY = 3
_TRUE_VALUES = {"1", "true", "yes", "on"}


def cloud_dispatch_enabled() -> bool:
    """Opt-in kill switch, default OFF — same idiom as
    ``risk_tier.risk_tier_enforcement_enabled`` (read at call time, never
    cached at import time, so a live config flip takes effect immediately)."""
    return (os.getenv(_ENV_ENABLED, "") or "").strip().lower() in _TRUE_VALUES


def cloud_shadow_enabled() -> bool:
    return cloud_dispatch_enabled() and (os.getenv(_ENV_SHADOW, "") or "").strip().lower() in _TRUE_VALUES


def cloud_max_concurrency() -> int:
    """Read at call time — same idiom as ``pipeline.review_fix_max_rounds``:
    a malformed value logs and falls back rather than raising, since a typo
    in an operator's env must not take the whole dispatcher down."""
    raw = (os.getenv(_ENV_MAX_CONCURRENCY) or "").strip()
    if not raw:
        return DEFAULT_MAX_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        logger.warning("claude_cloud: invalid %s=%r, using default %d",
                        _ENV_MAX_CONCURRENCY, raw, DEFAULT_MAX_CONCURRENCY)
        return DEFAULT_MAX_CONCURRENCY
    if value <= 0:
        logger.warning("claude_cloud: %s=%d must be positive, using default %d",
                        _ENV_MAX_CONCURRENCY, value, DEFAULT_MAX_CONCURRENCY)
        return DEFAULT_MAX_CONCURRENCY
    return value


def cloud_max_per_day() -> Optional[int]:
    """Optional UTC-day launch cap; unset means no daily cap."""
    raw = (os.getenv(_ENV_MAX_PER_DAY) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        logger.warning("claude_cloud: invalid %s=%r, ignoring daily cap",
                       _ENV_MAX_PER_DAY, raw)
        return None
    return value


def cloud_cli_path() -> str:
    return (os.getenv(_ENV_CLI_PATH) or "claude").strip() or "claude"


_ENV_STALE_SECONDS = "AGENT_CREW_CLOUD_STALE_SECONDS"
#: #499 r0 HIGH: a no-PR terminal outcome (ALREADY_FIXED/BLOCKED_FOR_CLOUD/
#: NEEDS_DECISION/FAILED with nothing pushed) cannot be observed via GitHub
#: at all — this is the backstop that keeps it from occupying a concurrency
#: slot forever. 4 hours comfortably exceeds every observed dispatch in the
#: issue's own cohort ledger (#487-#491, all resolved within ~1-2 hours).
DEFAULT_STALE_SECONDS = 4 * 3600


def cloud_stale_seconds() -> float:
    """Read at call time — same idiom as ``cloud_max_concurrency``."""
    raw = (os.getenv(_ENV_STALE_SECONDS) or "").strip()
    if not raw:
        return DEFAULT_STALE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning("claude_cloud: invalid %s=%r, using default %s",
                        _ENV_STALE_SECONDS, raw, DEFAULT_STALE_SECONDS)
        return DEFAULT_STALE_SECONDS
    if value <= 0:
        logger.warning("claude_cloud: %s=%s must be positive, using default %s",
                        _ENV_STALE_SECONDS, value, DEFAULT_STALE_SECONDS)
        return DEFAULT_STALE_SECONDS
    return value


# ---------------------------------------------------------------------------
# CLI capability probe (acceptance test 1) — fail closed on anything else.
# ---------------------------------------------------------------------------

RunFn = Callable[[List[str]], "subprocess.CompletedProcess"]


def _default_run(argv: List[str], *, timeout: float = 20.0):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


@dataclass(frozen=True)
class CloudCapability:
    supported: bool
    reason: str
    raw_output: str = ""


#: Required behavior #1: "never depend on parsing only human prose if a
#: stable CLI field/interface is available ... probe capabilities first and
#: fail closed if the output contract is unknown." The installed CLI (2.1.282)
#: has no machine-readable capabilities command (#496 checkpoint 1), so the
#: least assumption we can make is that its own documented flag list
#: (`--help`) names `--cloud` — not that any particular launch output shape
#: is stable.
_CLOUD_FLAG_RE = re.compile(r"(?m)^\s*--cloud\b")


def probe_cloud_cli(run_fn: RunFn = _default_run, *, cli_path: Optional[str] = None) -> CloudCapability:
    """Fail closed unless ``<cli_path> --help`` documents a ``--cloud`` flag."""
    path = cli_path or cloud_cli_path()
    try:
        result = run_fn([path, "--help"])
    except FileNotFoundError:
        return CloudCapability(False, f"cli not found: {path}")
    except Exception as exc:  # timeout, permission error, etc. — never crash the dispatcher
        return CloudCapability(False, f"probe failed: {exc}")
    if getattr(result, "returncode", 1) != 0:
        return CloudCapability(False, f"--help exited {result.returncode}", result.stdout or "")
    output = result.stdout or ""
    if not _CLOUD_FLAG_RE.search(output):
        return CloudCapability(False, "--cloud flag not found in --help output", output)
    return CloudCapability(True, "ok", output)


# ---------------------------------------------------------------------------
# Launch command construction and output parsing (acceptance tests 2, 3).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CloudLaunchResult:
    dispatched: bool
    session_id: Optional[str] = None
    session_url: Optional[str] = None
    raw_output: str = ""
    error: Optional[str] = None


_SESSION_ID_RE = re.compile(r"session_[A-Za-z0-9]{6,}")
_VIEW_URL_RE = re.compile(r"View:\s*(https://\S+)")


def parse_cloud_launch_output(stdout: str) -> CloudLaunchResult:
    """Fail closed: only claim a dispatch when BOTH a session id and a URL
    are unambiguously present.

    #496 launch receipt (session_01KCkrmLbhobrds8fApuuoun): ``--output-format
    json`` did NOT actually produce JSON on the installed CLI (2.1.282) — the
    real output was human-readable text (``Created cloud session: ...`` /
    ``View: https://...`` / ``Resume with: claude --teleport session_...``).
    JSON is tried first, for a CLI version that fixes this; the observed text
    shape is the fallback. Anything else — including a *changed* text shape
    that mentions neither — fails closed rather than guessing (acceptance
    test 3): the task is left for ``force_fail``/fallback, never silently
    marked dispatched.
    """
    text = stdout or ""
    stripped = text.strip()
    if stripped:
        try:
            payload = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict):
            session_id = payload.get("session_id") or payload.get("sessionId")
            session_url = payload.get("url") or payload.get("session_url")
            if (isinstance(session_id, str) and session_id
                    and isinstance(session_url, str) and session_url):
                return CloudLaunchResult(True, session_id, session_url, text)
            return CloudLaunchResult(False, raw_output=text, error="json_missing_session_fields")
    url_match = _VIEW_URL_RE.search(text)
    id_match = _SESSION_ID_RE.search(text)
    if url_match and id_match:
        return CloudLaunchResult(True, id_match.group(0), url_match.group(1), text)
    return CloudLaunchResult(False, raw_output=text, error="unrecognized_cli_output")


def build_launch_argv(prompt: str, *, cli_path: Optional[str] = None) -> List[str]:
    """Wrap in ``script`` for a pty.

    #496 launch receipt: the installed CLI refuses ``--cloud`` without an
    interactive terminal ("Error: --cloud requires an interactive terminal.
    Non-interactive invocations ... run locally and would silently ignore
    --cloud."). A silent local run is exactly the failure mode this backend
    must not have, so dispatch always goes through a pty wrapper rather than
    a bare non-interactive subprocess call.
    """
    path = cli_path or cloud_cli_path()
    inner = f"{path} --cloud {shlex.quote(prompt)} --output-format json"
    return ["script", "-qefc", inner, "/dev/null"]


def build_resume_argv(session_id: str, prompt: str, *, cli_path: Optional[str] = None) -> List[str]:
    """Best-evidenced shape for routing findings back to the SAME cloud
    session (required behavior #4 / acceptance test 6): ``claude --cloud
    <session_id> "<prompt>"``. Not independently confirmed by the coordinator
    ("Try the same-session fix continuation only after verifying the
    installed CLI behavior ... Record a precise fallback/hold if it is
    unsupported rather than claiming it works") — callers MUST treat a
    launch that does not parse as ``dispatch_cloud_for_role`` does: fail
    closed and fall back through the existing chain (acceptance test 8),
    never assume the resume silently worked.
    """
    path = cli_path or cloud_cli_path()
    inner = f"{path} --cloud {shlex.quote(session_id)} {shlex.quote(prompt)} --output-format json"
    return ["script", "-qefc", inner, "/dev/null"]


def build_cloud_task_prompt(task: TaskRequest, *, repo: str = "") -> str:
    """The bounded task contract handed to a fresh cloud session (required
    behavior #2).

    A cloud session does not inherit a local working directory, and has no
    network path back to this dispatcher's HTTP endpoint (#496 comment
    5880333100: no headless status read exists) — everything the local
    push-model conveys via cwd and the ``=== AGENT_CREW TASK ===`` block must
    be stated in the prompt text instead. Completion is later observed via
    GitHub (``reconcile_cloud_dispatch``), never by the session calling back
    in.
    """
    if task.task_type == "review":
        context = task.context if isinstance(task.context, dict) else {}
        pr_number = context.get("pr_number") or task.pr_number
        reviewed_sha = context.get("reviewed_sha") or ""
        return "\n".join([
            f"Review Agent Crew task {task.task_id}.",
            f"Repository: {repo}",
            f"PR: #{pr_number}",
            f"reviewed_sha {reviewed_sha}",
            "Description:", task.description,
            "Check out the exact reviewed_sha above, verify HEAD equals it, review the diff, "
            "and run the relevant tests. Post exactly ONE PR comment using "
            "gh pr comment (not gh pr review --comment) whose first line is "
            "[agent_crew review] verdict: approve|request_changes (choose one).",
            f"Include a line: reviewed_sha {reviewed_sha}",
            "For request_changes, list actionable findings as "
            "- HIGH|MED|LOW path:line - text. Every finding needs a verified "
            "path and line number; do not invent one. If a blocker cannot be "
            "anchored to a line, do not post a marked verdict; let local review "
            "handle it. For approve, include no findings. Put test notes in "
            "plain prose, with no other '- ' lines.",
            f"Include the exact marker line: <!-- agent_crew:cloud-review task={task.task_id} sha={reviewed_sha} -->",
            "No push, no merge, no GitHub review-state approval, and no file changes.",
        ])
    lines = [f"Implement Agent Crew task {task.task_id} ({task.task_type})."]
    if repo:
        lines.append(f"Repository: {repo}. Work only inside this repository.")
    if task.branch:
        lines.append(f"Push your work to branch: {task.branch}")
    lines.append("Description:")
    lines.append(task.description)
    lines.append(
        "Contract: inspect the current issue/repo state first; if the work is "
        "already done, stop and report ALREADY_FIXED without opening a PR. "
        "Bounded implementation only, with focused and surrounding tests. Open "
        "a dedicated PR with durable test evidence (tested HEAD SHA, exact "
        "commands, pass/fail counts, compared against base). Do not merge or "
        "deploy. Do not invent an architecture/owner decision — report "
        "NEEDS_DECISION instead. End your final report with exactly one line: "
        "PR_READY <url> | ALREADY_FIXED <evidence> | BLOCKED_FOR_CLOUD <reason> "
        "| NEEDS_DECISION <question> | FAILED <reason>."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dispatch orchestration (acceptance tests 2, 3, 6, 8, 9, 10, 11, 12).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CloudDispatchOutcome:
    """What happened when the dispatcher tried to hand one task to Cloud."""
    dispatched: bool
    task_id: Optional[str] = None
    session_id: Optional[str] = None
    session_url: Optional[str] = None
    #: Why nothing was dispatched: "disabled" | "at_capacity" |
    #: "cli_unsupported:<reason>" | "no_task" | "admission_refused".
    skipped_reason: Optional[str] = None
    #: Set only when a task WAS claimed (dequeued) but the launch failed
    #: closed — the task was handed to force_fail/fallback, not lost.
    launch_error: Optional[str] = None


def _resolve_resume_session_id(queue: TaskQueue, task: TaskRequest) -> Optional[str]:
    """Walk the SAME lineage chain ``pipeline._lineage_root_task_id`` already
    walks (``context.prev_task_id``) to find the original implement task for
    this fix, then read ITS persisted cloud session id. Reuses lineage
    tracking rather than reimplementing it; returns ``None`` (fresh session)
    on any lookup failure — a resume is an optimization, not a requirement.
    """
    ctx = task.context if isinstance(task.context, dict) else {}
    prev = ctx.get("prev_task_id")
    if not isinstance(prev, str) or not prev:
        return None
    try:
        from agent_crew.pipeline import _lineage_root_task_id
        tasks_by_id = {t.task_id: t for t in queue.list_tasks()}
        if task.task_id not in tasks_by_id:
            tasks_by_id[task.task_id] = task
        root_id = _lineage_root_task_id(tasks_by_id, task)
    except Exception:
        logger.exception("claude_cloud: lineage lookup failed for %s", task.task_id)
        return None
    if root_id == task.task_id:
        return None
    attribution = queue.get_attribution(root_id)
    session_id = (attribution or {}).get("provider_session_id") or ""
    return session_id or None


def _resolve_task_repo(task: TaskRequest, explicit_repo: Optional[str]) -> str:
    """#499 r0 HIGH: a cloud session does not inherit the dispatcher's cwd,
    so ``build_cloud_task_prompt`` must be told the repo explicitly — a bare
    ``repo=""`` silently produced a prompt with no repository at all.

    Precedence: an explicitly passed ``repo`` (caller override) > the task's
    OWN ``context["repo"]`` (the registered repo it was created against —
    the same key ``pipeline.auto_enqueue_review``/``_auto_enqueue_fix``
    already read) > ``github.get_repo()`` (the dispatcher process's own
    checkout — correct for a single-project deployment, which is what every
    existing call to ``get_repo()`` in this codebase already assumes).
    """
    if explicit_repo:
        return explicit_repo
    ctx = task.context if isinstance(task.context, dict) else {}
    ctx_repo = ctx.get("repo")
    if isinstance(ctx_repo, str) and ctx_repo.strip():
        return ctx_repo.strip()
    try:
        return _github.get_repo() or ""
    except Exception:
        logger.exception("claude_cloud: get_repo() failed while resolving prompt repo")
        return ""


def _fail_closed_dispatch(queue: TaskQueue, task: TaskRequest, task_type: str,
                          reason: str) -> CloudDispatchOutcome:
    """Fail closed without losing the task (acceptance test 3): force_fail
    records why, then the SAME rate-limit-shaped fallback local providers use
    gets a chance to reroute it — never a new fallback policy (acceptance
    test 8)."""
    logger.warning("claude_cloud: dispatch failed closed for %s — %s", task.task_id, reason)
    if (task.context or {}).get("cloud_session_id") or queue.get_task_context(task.task_id).get("cloud_session_id"):
        queue.patch_context(task.task_id, {"cloud_ended_at": time.time(),
                                           "cloud_terminal_error": reason})
    queue.force_fail(task.task_id, reason)
    result = TaskResult(task_id=task.task_id, status="failed", summary=reason[:2000])
    try:
        from agent_crew.pipeline import auto_fallback_failed_task
        auto_fallback_failed_task(queue, task.task_id, result, task_type)
    except Exception:
        logger.exception("claude_cloud: fallback failed for %s", task.task_id)
    return CloudDispatchOutcome(False, task_id=task.task_id, launch_error=reason)


def _claimable_cloud_reviews(queue: TaskQueue) -> list[TaskRequest]:
    now = time.time()

    def claimable(task: TaskRequest) -> bool:
        context = task.context if isinstance(task.context, dict) else {}
        if task.task_type != "review" or context.get("agent_override") not in (None, CLOUD_PROVIDER_NAME):
            return False
        try:
            return float(context.get("push_not_before") or 0) <= now
        except (TypeError, ValueError):
            return False

    return [task for task in queue.list_tasks(status="pending") if claimable(task)]


def cloud_review_skip_ids(queue: TaskQueue) -> set[str]:
    """Prefer the largest claimable review within the highest priority."""
    pending = _claimable_cloud_reviews(queue)
    if not pending:
        return set()

    def size(task: TaskRequest) -> int:
        context = task.context if isinstance(task.context, dict) else {}
        diff = next((context.get(key) for key in
                     ("diff_bytes", "review_diff_bytes", "diff_size")
                     if isinstance(context.get(key), int) and context.get(key) >= 0), None)
        context_size = len(str(context.get("instructions") or "")) + len(task.description)
        return max(diff or 0, context_size)

    best_priority = min(task.priority for task in pending)
    largest = max((task for task in pending if task.priority == best_priority), key=size)
    return {task.task_id for task in pending if task.task_id != largest.task_id}


_DISPATCH_PROBE_CACHE: dict[tuple[RunFn, str], tuple[float, CloudCapability]] = {}
_DISPATCH_PROBE_WARNING_AT: dict[str, float] = {}


def dispatch_cloud_for_role(
    queue: TaskQueue, *, role: str, task_type: str,
    repo: Optional[str] = None, run_fn: Optional[RunFn] = None,
    cli_path: Optional[str] = None,
) -> CloudDispatchOutcome:
    """Hand at most one pending task for ``role`` to a Cloud session.

    Mirrors ``server._try_push_next``'s gate ordering exactly — dequeue
    (STOP/pause, already enforced there) then ``record_dispatch`` (CEA
    admission) BEFORE anything provider-facing runs — but skips every
    tmux-specific concern (pane busy-check, context-clear-by-pane-capture,
    worktree-via-tmux measurement): a cloud session has no pane.
    """
    if not cloud_dispatch_enabled():
        return CloudDispatchOutcome(False, skipped_reason="disabled")

    if task_type == "review" and not _claimable_cloud_reviews(queue):
        return CloudDispatchOutcome(False, skipped_reason="no_task")

    in_flight = queue.count_in_progress_by_dispatch_channel(DISPATCH_CHANNEL)
    if in_flight >= cloud_max_concurrency():
        logger.debug("claude_cloud: at capacity (%d/%d) — skipping role=%s",
                     in_flight, cloud_max_concurrency(), role)
        return CloudDispatchOutcome(False, skipped_reason="at_capacity")

    if task_type == "review":
        daily_cap = cloud_max_per_day()
        if daily_cap is not None:
            day_start = int(time.time() // 86400) * 86400
            if queue.count_cloud_reviews_started_since(day_start) >= daily_cap:
                logger.info("claude_cloud: daily review cap %d reached; local fallback", daily_cap)
                return CloudDispatchOutcome(False, skipped_reason="daily_cap")

    run = run_fn or _default_run
    probe_key = (run, cli_path or cloud_cli_path())
    cached = _DISPATCH_PROBE_CACHE.get(probe_key) if run_fn is None else None
    if cached and time.monotonic() - cached[0] < 60:
        capability = cached[1]
    else:
        capability = probe_cloud_cli(run, cli_path=cli_path)
        if run_fn is None:
            _DISPATCH_PROBE_CACHE[probe_key] = (time.monotonic(), capability)
    if not capability.supported:
        warning_key = f"{probe_key[1]}:{capability.reason}"
        now = time.monotonic()
        if now - _DISPATCH_PROBE_WARNING_AT.get(warning_key, float("-inf")) >= 60:
            logger.warning("claude_cloud: CLI capability probe failed — %s", capability.reason)
            _DISPATCH_PROBE_WARNING_AT[warning_key] = now
        return CloudDispatchOutcome(False, skipped_reason=f"cli_unsupported:{capability.reason}")

    task = queue.dequeue(agent=CLOUD_PROVIDER_NAME, role=role,
                         claimed_via="cloud_push", skip_deferred=True,
                         skip_task_ids=cloud_review_skip_ids(queue) if task_type == "review" else None)
    if task is None:
        return CloudDispatchOutcome(False, skipped_reason="no_task")

    try:
        queue.record_dispatch(
            task.task_id, channel=DISPATCH_CHANNEL, agent=CLOUD_PROVIDER_NAME,
            target="pending", lease_owner=f"cloud:{task.task_id}",
        )
    except AdmissionRefused as exc:
        logger.warning("claude_cloud: dispatch refused for %s — %s", task.task_id, exc)
        return CloudDispatchOutcome(False, task_id=task.task_id, skipped_reason="admission_refused")

    if task.task_type == "review":
        context = task.context if isinstance(task.context, dict) else {}
        sha = context.get("reviewed_sha")
        pr = context.get("pr_number") or task.pr_number
        if (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha)
                or not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0):
            return _fail_closed_dispatch(queue, task, task_type,
                                         "cloud_review_missing_pinned_pr_or_sha")

    resume_session_id = _resolve_resume_session_id(queue, task)
    effective_repo = _resolve_task_repo(task, repo)
    if task.task_type == "review" and not effective_repo:
        return _fail_closed_dispatch(queue, task, task_type,
                                     "cloud_review_repo_unresolved")
    prompt = build_cloud_task_prompt(task, repo=effective_repo)
    argv = (build_resume_argv(resume_session_id, prompt, cli_path=cli_path)
            if resume_session_id else build_launch_argv(prompt, cli_path=cli_path))

    try:
        proc = run(argv)
    except Exception as exc:
        return _fail_closed_dispatch(queue, task, task_type, f"cloud_cli_invocation_error: {exc}")

    if getattr(proc, "returncode", 1) != 0:
        reason = f"cloud_cli_exit_{proc.returncode}: {(proc.stdout or '')[:500]}"
        return _fail_closed_dispatch(queue, task, task_type, reason)

    launch = parse_cloud_launch_output(proc.stdout or "")
    if not launch.dispatched:
        # Acceptance test 8: a continuation (resume) that fails closed is
        # NOT the same event as a fresh dispatch that fails closed — both
        # still route through the SAME existing fallback, just labeled
        # distinctly for operators reading the failure reason.
        prefix = "cloud_continuation_unavailable" if resume_session_id else "cloud_output_unrecognized"
        return _fail_closed_dispatch(queue, task, task_type, f"{prefix}: {launch.error}")

    execution_policy = "fresh_cloud_resume" if resume_session_id else "fresh_cloud"
    prev_task_id = ""
    if isinstance(task.context, dict):
        prev = task.context.get("prev_task_id")
        prev_task_id = prev if isinstance(prev, str) else ""
    launched_at = time.time()
    queue.record_attribution(
        task_id=task.task_id, project=task.project or "", agent=CLOUD_PROVIDER_NAME,
        role=role, task_type=task.task_type, worktree_path="", git_branch=task.branch or "",
        status="in_progress", provider_session_id=launch.session_id or "",
        previous_task_id=prev_task_id, started_at=launched_at,
    )
    queue.patch_context(task.task_id, {
        "cloud_session_url": launch.session_url,
        "cloud_session_id": launch.session_id,
        "cloud_launched_at": launched_at,
        "execution_policy": execution_policy,
    })
    queue.set_push_at(task.task_id, pane_id=f"cloud:{launch.session_id}")
    logger.info("claude_cloud: dispatched %s session_id=%s url=%s",
                task.task_id, launch.session_id, launch.session_url)
    return CloudDispatchOutcome(True, task_id=task.task_id, session_id=launch.session_id,
                                session_url=launch.session_url)


def launch_shadow_review(queue: TaskQueue, task: TaskRequest, *, run_fn: Optional[RunFn] = None) -> bool:
    """Launch a comparison review without claiming or completing the local task."""
    if not cloud_shadow_enabled() or task.task_type != "review":
        return False
    current = queue.get_task_context(task.task_id)
    if current.get("cloud_shadow_launch_reserved_at") or current.get("cloud_shadow_session_id"):
        return False
    shadow_active = sum(1 for row in queue.list_tasks()
                        if (row.context or {}).get("cloud_shadow_session_id")
                        and not (row.context or {}).get("cloud_shadow_ended_at"))
    if shadow_active + queue.count_in_progress_by_dispatch_channel(DISPATCH_CHANNEL) >= cloud_max_concurrency():
        return False
    daily_cap = cloud_max_per_day()
    if daily_cap is not None:
        day_start = int(time.time() // 86400) * 86400
        if queue.count_cloud_reviews_started_since(day_start) >= daily_cap:
            return False
    context = task.context if isinstance(task.context, dict) else {}
    if not isinstance(context.get("pr_number") or task.pr_number, int) or not re.fullmatch(
            r"[0-9a-fA-F]{40}", str(context.get("reviewed_sha") or "")):
        return False
    run = run_fn or _default_run
    if not probe_cloud_cli(run).supported:
        return False
    repo = _resolve_task_repo(task, None)
    if not repo:
        return False
    if not queue.reserve_cloud_shadow_launch(task.task_id):
        return False
    try:
        proc = run(build_launch_argv(build_cloud_task_prompt(task, repo=repo)))
        launch = parse_cloud_launch_output(proc.stdout or "") if proc.returncode == 0 else None
    except Exception:
        logger.exception("claude_cloud: shadow launch failed for %s", task.task_id)
        queue.patch_context(task.task_id, {"cloud_shadow_status": "failed",
                                           "cloud_shadow_ended_at": time.time()})
        return False
    if not launch or not launch.dispatched:
        queue.patch_context(task.task_id, {"cloud_shadow_status": "failed",
                                           "cloud_shadow_ended_at": time.time()})
        return False
    queue.patch_context(task.task_id, {
        "cloud_shadow_session_id": launch.session_id,
        "cloud_shadow_session_url": launch.session_url,
        "cloud_shadow_launched_at": time.time(),
        "cloud_shadow_status": "in_progress",
    })
    return True


def reconcile_shadow_reviews(queue: TaskQueue, *, pr_comments_fn=None) -> None:
    """Record comparison verdicts in task context; never submit a result."""
    comments_for_pr = pr_comments_fn or _github.pr_comments
    for task in queue.list_tasks():
        context = task.context if isinstance(task.context, dict) else {}
        if not context.get("cloud_shadow_session_id"):
            continue
        result = queue.get_result(task.task_id)
        if result and result.verdict and not context.get("cloud_shadow_local_verdict"):
            queue.patch_context(task.task_id, {"cloud_shadow_local_verdict": result.verdict})
        if context.get("cloud_shadow_ended_at"):
            continue
        pr = context.get("pr_number") or task.pr_number
        sha = context.get("reviewed_sha") or ""
        try:
            comments = comments_for_pr(pr, repo=_resolve_task_repo(task, None)) or []
            marker = f"<!-- agent_crew:cloud-review task={task.task_id} "
            matches = [str(row.get("body") or "") for row in comments
                       if isinstance(row, dict) and marker in str(row.get("body") or "")]
            if matches:
                if len(matches) != 1:
                    raise ValueError("multiple marked comments")
                review = parse_cloud_review_comment(matches[0], task_id=task.task_id,
                                                    reviewed_sha=sha, pr_number=pr)
                queue.patch_context(task.task_id, {
                    "cloud_shadow_status": "completed",
                    "cloud_shadow_verdict": review.verdict,
                    "cloud_shadow_findings": review.findings,
                    "cloud_shadow_ended_at": time.time(),
                })
                continue
        except ValueError as exc:
            queue.patch_context(task.task_id, {"cloud_shadow_status": "failed",
                                              "cloud_shadow_error": str(exc),
                                              "cloud_shadow_ended_at": time.time()})
            continue
        except Exception:
            logger.exception("claude_cloud: shadow reconciliation failed for %s", task.task_id)
        if time.time() - float(context.get("cloud_shadow_launched_at") or 0) > cloud_stale_seconds():
            queue.patch_context(task.task_id, {"cloud_shadow_status": "failed",
                                              "cloud_shadow_error": "stale: no marked review",
                                              "cloud_shadow_ended_at": time.time()})


# ---------------------------------------------------------------------------
# Completion reconciliation — GitHub-only, no live callback (documented gap).
# ---------------------------------------------------------------------------

#: The five terminal outcomes this backend's task contract requires
#: (required behavior #2). Mapped onto the EXISTING `TaskResult.status`
#: vocabulary (`protocol._VALID_RESULT_STATUSES`) rather than inventing a
#: parallel enum — PR_READY is handled separately, via PR discovery, because
#: it is the one outcome GitHub can confirm independently of this text.
_OUTCOME_STATUS = {
    "ALREADY_FIXED": "completed",
    "BLOCKED_FOR_CLOUD": "blocked",
    "NEEDS_DECISION": "needs_human",
    "FAILED": "failed",
}
_TERMINAL_LINE_RE = re.compile(
    r"(?m)^\s*(PR_READY|ALREADY_FIXED|BLOCKED_FOR_CLOUD|NEEDS_DECISION|FAILED)\b(.*)$"
)


def parse_terminal_outcome(text: str) -> Optional[tuple]:
    """``(TOKEN, rest_of_line)`` for the LAST matching line in ``text``, or
    ``None`` if no recognized token appears anywhere — fail closed, exactly
    like this session's own final-line contract. Only the last match counts:
    a session's own prompt/instructions (echoed in a transcript) can contain
    every token as prose; its actual verdict is the one it ends on."""
    matches = list(_TERMINAL_LINE_RE.finditer(text or ""))
    if not matches:
        return None
    m = matches[-1]
    return (m.group(1), m.group(2).strip())


@dataclass(frozen=True)
class CloudReconciliationOutcome:
    task_id: str
    #: "pr_ready" | "already_fixed" | "blocked_for_cloud" | "needs_decision"
    #: | "failed" | "still_dispatched" | "unknown_outcome"
    action: str
    pr_number: Optional[int] = None
    detail: str = ""


_REVIEW_MARKER_RE = re.compile(
    r"<!-- agent_crew:cloud-review task=([^\s>]+) sha=([^\s>]+) -->")
_REVIEW_VERDICT_RE = re.compile(
    r"\A\[agent_crew review\] verdict: (approve|request_changes)\s*$")
_REVIEW_FINDING_RE = re.compile(
    r"^- (HIGH|MED|LOW) ([^\s:]+):(\d+) - (.+)$")


def parse_cloud_review_comment(body: str, *, task_id: str, reviewed_sha: str,
                               pr_number: int) -> TaskResult:
    """Accept one pinned, actionable cloud review comment or raise ValueError."""
    markers = _REVIEW_MARKER_RE.findall(body)
    if markers != [(task_id, reviewed_sha)]:
        raise ValueError("cloud review marker task/SHA mismatch or ambiguity")
    lines = body.splitlines()
    if not lines:
        raise ValueError("cloud review comment is empty")
    verdict_match = _REVIEW_VERDICT_RE.fullmatch(lines[0])
    if verdict_match is None:
        raise ValueError("cloud review verdict line is malformed")
    sha_lines = [line for line in lines if line.startswith("reviewed_sha ")]
    if sha_lines != [f"reviewed_sha {reviewed_sha}"]:
        raise ValueError("cloud review reviewed_sha line does not match")
    findings = []
    for line in lines[1:]:
        if line.startswith("- "):
            if _REVIEW_FINDING_RE.fullmatch(line) is None:
                raise ValueError("cloud review finding is malformed")
            findings.append(line[2:])
    verdict = verdict_match.group(1)
    if (verdict == "approve" and findings) or (verdict == "request_changes" and not findings):
        raise ValueError("cloud review verdict/findings disagree")
    return TaskResult(task_id=task_id, status="completed", verdict=verdict,
                      summary=f"Cloud review of PR #{pr_number} at {reviewed_sha}: " +
                              "\n".join(line for line in lines[1:] if not line.startswith("<!--"))[:3500],
                      findings=findings, pr_number=pr_number)


def _reconcile_cloud_review(
    queue: TaskQueue, task: TaskRequest, *, repo: str,
    pr_comments_fn, submit_review_result_fn,
) -> CloudReconciliationOutcome:
    context = task.context if isinstance(task.context, dict) else {}
    pr_number = context.get("pr_number") or task.pr_number
    reviewed_sha = context.get("reviewed_sha") or ""
    retry_detail = ""
    if (not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0
            or not isinstance(reviewed_sha, str)
            or not re.fullmatch(r"[0-9a-fA-F]{40}", reviewed_sha)):
        _fail_closed_dispatch(queue, task, "review", "cloud_review_missing_pinned_pr_or_sha")
        return CloudReconciliationOutcome(task.task_id, "failed", detail="missing pinned PR/SHA")
    comments = pr_comments_fn(pr_number, repo=repo)
    if comments is not None:
        prefix = f"<!-- agent_crew:cloud-review task={task.task_id} "
        matches = [c.get("body", "") for c in comments if isinstance(c, dict)
                   and prefix in str(c.get("body") or "")]
        if matches:
            try:
                if len(matches) != 1:
                    raise ValueError("multiple cloud review comments for task")
                result = parse_cloud_review_comment(
                    matches[0], task_id=task.task_id, reviewed_sha=reviewed_sha,
                    pr_number=pr_number)
            except ValueError as exc:
                _fail_closed_dispatch(queue, task, "review", f"cloud_review_invalid_comment: {exc}")
                return CloudReconciliationOutcome(task.task_id, "failed", pr_number=pr_number,
                                                  detail=str(exc))
            if submit_review_result_fn is None:
                retry_detail = "result handler unavailable"
            else:
                # The server's normal result handler consumes this flag to avoid
                # reposting the cloud session's already published review comment.
                queue.patch_context(task.task_id, {"cloud_review_comment_confirmed": True})
                try:
                    acknowledgement = submit_review_result_fn(task.task_id, result)
                except Exception:
                    logger.exception("claude_cloud: review result handler failed for %s", task.task_id)
                    retry_detail = "result handler failed; will retry"
                else:
                    if isinstance(acknowledgement, dict) and acknowledgement.get("held"):
                        retry_detail = f"result held: {acknowledgement['held']}"
                    else:
                        queue.patch_context(task.task_id, {
                            "cloud_ended_at": time.time(),
                            "cloud_review_verdict": result.verdict,
                            "cloud_review_findings": result.findings,
                        })
                        return CloudReconciliationOutcome(task.task_id, "review_completed",
                                                          pr_number=pr_number)
    age = time.time() - queue.get_dispatched_at(task.task_id)
    if age > cloud_stale_seconds():
        _fail_closed_dispatch(queue, task, "review",
                              f"cloud_review_stale: {retry_detail or 'no marked comment'} "
                              f"after {int(age)}s")
        return CloudReconciliationOutcome(task.task_id, "failed", pr_number=pr_number,
                                          detail="stale cloud review")
    return CloudReconciliationOutcome(task.task_id, "still_dispatched", pr_number=pr_number,
                                      detail=retry_detail)


def reconcile_cloud_dispatch(
    queue: TaskQueue, task: TaskRequest, *, repo: Optional[str] = None,
    pr_number_for_branch_fn=None, pr_head_sha_fn=None, commit_message_fn=None,
    pr_state_fn=None, pr_comments_fn=None, submit_review_result_fn=None,
) -> CloudReconciliationOutcome:
    """GitHub-only completion detection for one dispatched cloud task.

    #496 checkpoint (comment 5880333100): "the installed CLI has no
    read-only status command for cloud sessions ... The #496 adapter needs
    either a way to read session status or GitHub-only completion
    detection." This is that detection, built entirely from existing/added
    github.py reads:

    1. A PR for the task's branch → acceptance test 5: create the SAME
       review task ``pipeline.auto_enqueue_review`` would for a local
       provider's completed result BEFORE marking this task completed
       (#499 r0 HIGH — see the note below), so the existing review pipeline
       picks it up completely unchanged.
    2. No PR, but the branch's HEAD commit carries one of the other four
       terminal tokens → recorded via the SAME ``submit_result``, with NO PR
       required (acceptance test 4), and a failed outcome still goes through
       the SAME ``auto_fallback_failed_task`` a local failure would.
    3. Neither, and the dispatch is still within its staleness budget →
       ``still_dispatched``. Past the budget (``cloud_stale_seconds``), the
       task is resolved to ``needs_human`` anyway (#499 r0 HIGH) — a session
       with no pushed branch at all (e.g. a true ALREADY_FIXED that touched
       nothing) is genuinely unobservable via GitHub, and leaving it
       in_progress forever would both hide the outcome and permanently
       occupy a concurrency slot.

    #499 r0 HIGH: ``auto_enqueue_review`` is called BEFORE ``submit_result``
    here, not after. It reads the task's CURRENT row (branch/context) plus
    the ``result`` argument directly — it does not require the task to
    already be marked completed — so calling it first costs nothing. Calling
    it after would let an enqueue failure (it swallows its own exceptions and
    returns ``None`` for "cross-project guard, missing impl task, exception"
    alike) mark the task ``completed`` with NO review ever created and NO
    way to retry, since a completed task no longer appears in
    ``list_in_progress_by_dispatch_channel``. ``auto_enqueue_review`` mints a
    deterministic review task id, so retrying it on the next pass is a safe
    no-op once the review already exists.
    """
    pr_number_for_branch_fn = pr_number_for_branch_fn or _github.pr_number_for_branch
    pr_head_sha_fn = pr_head_sha_fn or _github.pr_head_sha
    commit_message_fn = commit_message_fn or _github.branch_head_commit_message
    pr_state_fn = pr_state_fn or _github.pr_state

    # #499 r1 HIGH: resolve THIS task's own repo (context["repo"], the same
    # key dispatch already reads via `_resolve_task_repo`) before any GitHub
    # lookup. `reconcile_all_cloud_tasks` reconciles a batch of tasks that
    # may span more than one repo; a single `repo`/`None` applied to every
    # task fell through to `github.get_repo()` (the dispatcher's OWN cwd),
    # which can name a different repository than the task's — silently
    # missing the real PR or matching one in the wrong repo entirely.
    effective_repo = _resolve_task_repo(task, repo)

    if task.task_type == "review":
        return _reconcile_cloud_review(
            queue, task, repo=effective_repo,
            pr_comments_fn=pr_comments_fn or _github.pr_comments,
            submit_review_result_fn=submit_review_result_fn,
        )

    branch = task.branch or ""
    if not branch:
        return CloudReconciliationOutcome(task.task_id, "unknown_outcome", detail="no branch on task")

    pr_number = pr_number_for_branch_fn(branch, repo=effective_repo)
    if pr_number:
        head_sha = pr_head_sha_fn(pr_number, repo=effective_repo) or ""
        result = TaskResult(
            task_id=task.task_id, status="completed",
            summary=f"claude_cloud: PR #{pr_number} discovered for branch {branch}",
            pr_number=pr_number, branch=branch, commit=head_sha,
        )
        try:
            from agent_crew.pipeline import auto_enqueue_review
            review_id = auto_enqueue_review(queue, task.task_id, pr_number,
                                            pr_state_fn=pr_state_fn, result=result)
        except Exception:
            logger.exception("claude_cloud: auto_enqueue_review raised for %s", task.task_id)
            review_id = None
        if review_id is None:
            logger.warning(
                "claude_cloud: auto_enqueue_review did not confirm a review for %s "
                "(pr=%s) — leaving in_progress to retry on the next pass",
                task.task_id, pr_number)
            return CloudReconciliationOutcome(
                task.task_id, "still_dispatched", pr_number=pr_number,
                detail="review not yet confirmed for discovered PR; will retry")
        queue.submit_result(task.task_id, result)
        return CloudReconciliationOutcome(task.task_id, "pr_ready", pr_number=pr_number)

    message = commit_message_fn(branch, repo=effective_repo)
    outcome = parse_terminal_outcome(message) if message else None
    if outcome is None:
        age = time.time() - queue.get_dispatched_at(task.task_id)
        if age > cloud_stale_seconds():
            stale_result = TaskResult(
                task_id=task.task_id, status="needs_human",
                summary=(f"claude_cloud: no PR and no recognized outcome for branch "
                        f"{branch} after {int(age)}s (budget {int(cloud_stale_seconds())}s) — "
                        f"unobservable via GitHub, needs manual investigation")[:4000],
                branch=branch,
            )
            queue.submit_result(task.task_id, stale_result)
            return CloudReconciliationOutcome(task.task_id, "needs_decision",
                                              detail="stale: no observable outcome")
        return CloudReconciliationOutcome(task.task_id, "still_dispatched",
                                          detail=f"no PR yet for branch {branch}")

    token, detail = outcome
    if token == "PR_READY":
        # Claimed PR_READY but no PR was actually discoverable — do not
        # trust prose over the GitHub read; stay dispatched rather than
        # fabricate a completion with no pr_number.
        return CloudReconciliationOutcome(task.task_id, "still_dispatched",
                                          detail="PR_READY claimed but no PR found")

    status = _OUTCOME_STATUS[token]
    result = TaskResult(task_id=task.task_id, status=status,
                        summary=f"claude_cloud: {token} {detail}"[:4000], branch=branch)
    queue.submit_result(task.task_id, result)
    if status == "failed":
        try:
            from agent_crew.pipeline import auto_fallback_failed_task
            auto_fallback_failed_task(queue, task.task_id, result, task.task_type)
        except Exception:
            logger.exception("claude_cloud: fallback failed for %s", task.task_id)
    action = {
        "ALREADY_FIXED": "already_fixed", "BLOCKED_FOR_CLOUD": "blocked_for_cloud",
        "NEEDS_DECISION": "needs_decision", "FAILED": "failed",
    }[token]
    return CloudReconciliationOutcome(task.task_id, action, detail=detail)


def reconcile_all_cloud_tasks(queue: TaskQueue, *, repo: Optional[str] = None,
                              submit_review_result_fn=None,
                              ) -> List[CloudReconciliationOutcome]:
    """Reconcile every currently in_progress ``claude_cloud`` dispatch.

    Called from ``server._watchdog_tick`` on its existing periodic loop — no
    polling loop of its own here.

    ``repo`` is an optional override applied to every task in this batch
    (mainly for callers/tests that already know a single answer). Left
    unset (the production/watchdog call), each task resolves its OWN repo
    from ``context["repo"]`` inside ``reconcile_cloud_dispatch`` (#499 r1
    HIGH) — a batch can span more than one repo, and a single fallback
    would silently apply the dispatcher's own cwd to every task instead.
    """
    tasks = queue.list_in_progress_by_dispatch_channel(DISPATCH_CHANNEL)
    return [reconcile_cloud_dispatch(queue, t, repo=repo,
                                     submit_review_result_fn=submit_review_result_fn)
            for t in tasks]
