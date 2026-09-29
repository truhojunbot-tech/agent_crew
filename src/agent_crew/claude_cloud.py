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
_ENV_CLI_PATH = "AGENT_CREW_CLOUD_CLI_PATH"
DEFAULT_MAX_CONCURRENCY = 3
_TRUE_VALUES = {"1", "true", "yes", "on"}


def cloud_dispatch_enabled() -> bool:
    """Opt-in kill switch, default OFF — same idiom as
    ``risk_tier.risk_tier_enforcement_enabled`` (read at call time, never
    cached at import time, so a live config flip takes effect immediately)."""
    return (os.getenv(_ENV_ENABLED, "") or "").strip().lower() in _TRUE_VALUES


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


def cloud_cli_path() -> str:
    return (os.getenv(_ENV_CLI_PATH) or "claude").strip() or "claude"


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


def _fail_closed_dispatch(queue: TaskQueue, task: TaskRequest, task_type: str,
                          reason: str) -> CloudDispatchOutcome:
    """Fail closed without losing the task (acceptance test 3): force_fail
    records why, then the SAME rate-limit-shaped fallback local providers use
    gets a chance to reroute it — never a new fallback policy (acceptance
    test 8)."""
    logger.warning("claude_cloud: dispatch failed closed for %s — %s", task.task_id, reason)
    queue.force_fail(task.task_id, reason)
    result = TaskResult(task_id=task.task_id, status="failed", summary=reason[:2000])
    try:
        from agent_crew.pipeline import auto_fallback_failed_task
        auto_fallback_failed_task(queue, task.task_id, result, task_type)
    except Exception:
        logger.exception("claude_cloud: fallback failed for %s", task.task_id)
    return CloudDispatchOutcome(False, task_id=task.task_id, launch_error=reason)


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

    in_flight = queue.count_in_progress_by_dispatch_channel(DISPATCH_CHANNEL)
    if in_flight >= cloud_max_concurrency():
        logger.debug("claude_cloud: at capacity (%d/%d) — skipping role=%s",
                     in_flight, cloud_max_concurrency(), role)
        return CloudDispatchOutcome(False, skipped_reason="at_capacity")

    run = run_fn or _default_run
    capability = probe_cloud_cli(run, cli_path=cli_path)
    if not capability.supported:
        logger.warning("claude_cloud: CLI capability probe failed — %s", capability.reason)
        return CloudDispatchOutcome(False, skipped_reason=f"cli_unsupported:{capability.reason}")

    task = queue.dequeue(role=role, claimed_via="cloud_push", skip_deferred=True)
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

    resume_session_id = _resolve_resume_session_id(queue, task)
    prompt = build_cloud_task_prompt(task, repo=repo or "")
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
    queue.record_attribution(
        task_id=task.task_id, project=task.project or "", agent=CLOUD_PROVIDER_NAME,
        role=role, task_type=task.task_type, worktree_path="", git_branch=task.branch or "",
        status="in_progress", provider_session_id=launch.session_id or "",
        previous_task_id=prev_task_id, started_at=time.time(),
    )
    queue.patch_context(task.task_id, {
        "cloud_session_url": launch.session_url,
        "execution_policy": execution_policy,
    })
    queue.set_push_at(task.task_id, pane_id=f"cloud:{launch.session_id}")
    logger.info("claude_cloud: dispatched %s session_id=%s url=%s",
                task.task_id, launch.session_id, launch.session_url)
    return CloudDispatchOutcome(True, task_id=task.task_id, session_id=launch.session_id,
                                session_url=launch.session_url)


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


def reconcile_cloud_dispatch(
    queue: TaskQueue, task: TaskRequest, *, repo: Optional[str] = None,
    pr_number_for_branch_fn=None, pr_head_sha_fn=None, commit_message_fn=None,
    pr_state_fn=None,
) -> CloudReconciliationOutcome:
    """GitHub-only completion detection for one dispatched cloud task.

    #496 checkpoint (comment 5880333100): "the installed CLI has no
    read-only status command for cloud sessions ... The #496 adapter needs
    either a way to read session status or GitHub-only completion
    detection." This is that detection, built entirely from existing/added
    github.py reads:

    1. A PR for the task's branch → acceptance test 5: submit the SAME
       ``TaskResult`` shape a local provider would, then call the SAME
       ``pipeline.auto_enqueue_review`` — the existing review pipeline picks
       it up completely unchanged.
    2. No PR, but the branch's HEAD commit carries one of the other four
       terminal tokens → recorded via the SAME ``submit_result``, with NO PR
       required (acceptance test 4), and a failed outcome still goes through
       the SAME ``auto_fallback_failed_task`` a local failure would.
    3. Neither → ``still_dispatched``; a session with no pushed branch at all
       (e.g. a true ALREADY_FIXED that touched nothing) is not observable
       this way — see the module docstring's documented gap.
    """
    pr_number_for_branch_fn = pr_number_for_branch_fn or _github.pr_number_for_branch
    pr_head_sha_fn = pr_head_sha_fn or _github.pr_head_sha
    commit_message_fn = commit_message_fn or _github.branch_head_commit_message
    pr_state_fn = pr_state_fn or _github.pr_state

    branch = task.branch or ""
    if not branch:
        return CloudReconciliationOutcome(task.task_id, "unknown_outcome", detail="no branch on task")

    pr_number = pr_number_for_branch_fn(branch, repo=repo)
    if pr_number:
        head_sha = pr_head_sha_fn(pr_number, repo=repo) or ""
        result = TaskResult(
            task_id=task.task_id, status="completed",
            summary=f"claude_cloud: PR #{pr_number} discovered for branch {branch}",
            pr_number=pr_number, branch=branch, commit=head_sha,
        )
        queue.submit_result(task.task_id, result)
        try:
            from agent_crew.pipeline import auto_enqueue_review
            auto_enqueue_review(queue, task.task_id, pr_number, pr_state_fn=pr_state_fn)
        except Exception:
            logger.exception("claude_cloud: auto_enqueue_review failed for %s", task.task_id)
        return CloudReconciliationOutcome(task.task_id, "pr_ready", pr_number=pr_number)

    message = commit_message_fn(branch, repo=repo)
    outcome = parse_terminal_outcome(message) if message else None
    if outcome is None:
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


def reconcile_all_cloud_tasks(queue: TaskQueue, *, repo: Optional[str] = None
                              ) -> List[CloudReconciliationOutcome]:
    """Reconcile every currently in_progress ``claude_cloud`` dispatch.

    Not wired to a scheduler here — see the module docstring. An operator or
    the coordinator calls this from whatever periodic mechanism already
    exists (cron, `crew triage --watch`); this function contains no polling
    loop of its own.
    """
    tasks = queue.list_in_progress_by_dispatch_channel(DISPATCH_CHANNEL)
    return [reconcile_cloud_dispatch(queue, t, repo=repo) for t in tasks]
