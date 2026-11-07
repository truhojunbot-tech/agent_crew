"""``chatgpt``: a reviewer-only backend that asks ChatGPT on GitHub (#1000).

Selected by ``role_agents.reviewer = "chatgpt"``. There is no tmux pane and no
local process: a dispatched review posts one ``[REVIEW REQUEST]`` comment and
waits, in_progress, for a strict ``[CHATGPT][CREW-REVIEW]`` reply on the same
thread. The lifecycle is the ``claude_cloud`` reviewer's (#712), not a new one:

- ``TaskQueue.dequeue`` / ``record_dispatch`` -- the same STOP/pause and CEA
  admission gates, and a dispatch channel of its own so the pane watchdog and
  the startup orphan sweep leave the row alone (claim source is not
  ``dispatcher``);
- a lease of ``timeout + poll`` seconds, so the existing orphan-lease reaper
  is the backstop if this reconciler ever stops running;
- the review result goes through the server's own ``submit_result``, so the
  verdict, the PR comment and the fix round are the same as any reviewer's;
- ``coordinator_notice_once`` for every fail-closed end.

The reply is accepted only on its header and VERDICT line (#936: a verdict read
from prose broke merges). Every ambiguity ends ``needs_human``; silence ends
``timed_out``. Nothing here ever turns into an approve by default.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional

from agent_crew import github as _github
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import AdmissionRefused, TaskQueue

logger = logging.getLogger(__name__)

PROVIDER_NAME = "chatgpt"
DISPATCH_CHANNEL = "chatgpt_review"
REQUEST_PREFIX = "[REVIEW REQUEST]"
REPLY_PREFIX = "[CHATGPT][CREW-REVIEW]"
VERDICTS = ("approve", "request_changes")

_ENV_CARRIER = "AGENT_CREW_CHATGPT_REVIEW_CARRIER"
_ENV_INTAKE = "AGENT_CREW_CHATGPT_REVIEW_INTAKE"
_ENV_POLL_S = "AGENT_CREW_CHATGPT_REVIEW_POLL_S"
_ENV_TIMEOUT_S = "AGENT_CREW_CHATGPT_REVIEW_TIMEOUT_S"
DEFAULT_CARRIER = "intake"
DEFAULT_POLL_SECONDS = 300.0
DEFAULT_TIMEOUT_SECONDS = 43200.0

_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
_INTAKE_RE = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9]\d*)")
_HEADER_RE = re.compile(r"\[CHATGPT\]\[CREW-REVIEW\] task=(\S+) sha=(\S+)")
_VERDICT_RE = re.compile(r"VERDICT: (approve|request_changes)")


def _positive_env(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(raw)
    except ValueError:
        logger.warning("chatgpt_review: invalid %s=%r, using default %s", name, raw, default)
        return default
    return value


def poll_seconds() -> float:
    return _positive_env(_ENV_POLL_S, DEFAULT_POLL_SECONDS)


def timeout_seconds() -> float:
    return _positive_env(_ENV_TIMEOUT_S, DEFAULT_TIMEOUT_SECONDS)


def request_thread(repo: str, pr_number: int) -> tuple[Optional[tuple[str, int]], str]:
    """``((repo, number), "")`` for the thread a request goes to, or
    ``(None, reason)``. An unusable carrier refuses the dispatch; it never
    falls back to another thread."""
    carrier = (os.getenv(_ENV_CARRIER) or DEFAULT_CARRIER).strip().lower()
    if carrier == "pr":
        return (repo, pr_number), ""
    if carrier != "intake":
        return None, f"{_ENV_CARRIER}={carrier!r} is not 'intake' or 'pr'"
    intake = (os.getenv(_ENV_INTAKE) or "").strip()
    match = _INTAKE_RE.fullmatch(intake)
    if match is None:
        return None, (f"{_ENV_CARRIER}=intake needs {_ENV_INTAKE}=owner/repo#N "
                      f"(got {intake!r})")
    return (match.group(1), int(match.group(2))), ""


# ---------------------------------------------------------------------------
# GitHub reads/writes. Tests hand in a fake with the same four methods.
# ---------------------------------------------------------------------------

class GhClient:
    """The four ``gh api`` calls this backend makes; ``None`` = unavailable."""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def _api(self, args: List[str], stdin: Optional[str] = None) -> Optional[str]:
        try:
            proc = subprocess.run(["gh", "api", *args], input=stdin, capture_output=True,
                                  text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired):
            logger.exception("chatgpt_review: gh api %s failed", args[:2])
            return None
        if proc.returncode != 0:
            logger.warning("chatgpt_review: gh api %s exit=%s %s", args[:2], proc.returncode,
                           (proc.stderr or "")[:300])
            return None
        return proc.stdout

    def pull(self, repo: str, number: int) -> Optional[dict]:
        out = self._api([f"repos/{_github._repo_slug(repo)}/pulls/{number}"])
        try:
            return json.loads(out) if out else None
        except ValueError:
            return None

    def pull_files(self, repo: str, number: int) -> Optional[list[str]]:
        out = self._api(["--paginate", f"repos/{_github._repo_slug(repo)}/pulls/{number}/files",
                         "--jq", ".[].filename"])
        return None if out is None else [line for line in out.splitlines() if line.strip()]

    def comments(self, repo: str, number: int) -> Optional[list[dict]]:
        out = self._api(["--paginate", f"repos/{_github._repo_slug(repo)}/issues/{number}/comments",
                         "--jq", ".[] | {id: .id, body: .body, html_url: .html_url, "
                         "created_at: .created_at}"])
        if out is None:
            return None
        try:
            return [json.loads(line) for line in out.splitlines() if line.strip()]
        except ValueError:
            return None

    def post_comment(self, repo: str, number: int, body: str) -> Optional[dict]:
        out = self._api(["-X", "POST", f"repos/{_github._repo_slug(repo)}/issues/{number}/comments",
                         "--input", "-"], stdin=json.dumps({"body": body}))
        try:
            record = json.loads(out) if out else None
        except ValueError:
            return None
        return record if isinstance(record, dict) and record.get("id") else None


# ---------------------------------------------------------------------------
# Request and reply formats.
# ---------------------------------------------------------------------------

def reply_header(task_id: str, sha: str) -> str:
    return f"{REPLY_PREFIX} task={task_id} sha={sha}"


def build_request_comment(*, project: str, task_id: str, pr_number: int, pr_url: str,
                          head_sha: str, base_sha: str, issue_link: str, acceptance: str,
                          files: list[str], suite_counts: list[str]) -> str:
    lines = [
        f"{REQUEST_PREFIX}[crew:{project}] task={task_id} pr=#{pr_number} "
        f"sha={head_sha} base={base_sha}",
        "",
        f"PR: {pr_url}",
        f"Issue: {issue_link or '(none recorded)'}",
        "",
        "## Acceptance (from the implement task)",
        acceptance.strip() or "(none recorded)",
        "",
        f"## Changed files ({len(files)})",
        *([f"- {name}" for name in files] or ["(none reported)"]),
        "",
        "## Full-suite counts (from the PR body)",
        *(suite_counts or ["(not found in the PR body)"]),
        "",
        "## Required reply format",
        "Reply on this thread. The first line must be exactly:",
        "```",
        reply_header(task_id, head_sha),
        "```",
        "and exactly one line must be `VERDICT: approve` or `VERDICT: request_changes`.",
        "Findings follow as free text. Review this sha only; a reply naming any other "
        "sha, or with no/several VERDICT lines, is not accepted and goes to a human.",
    ]
    return "\n".join(lines)


_SUITE_RE = re.compile(r"\b\d+\s+(passed|failed)\b", re.IGNORECASE)


def suite_count_lines(pr_body: str) -> list[str]:
    return [line.strip() for line in (pr_body or "").splitlines() if _SUITE_RE.search(line)][:10]


@dataclass(frozen=True)
class ReplyParse:
    #: "foreign" (not a reply to this task) | "accepted" | "rejected"
    kind: str
    verdict: Optional[str] = None
    text: str = ""
    reason: str = ""


def parse_reply(body: str, *, task_id: str, sha: str) -> ReplyParse:
    """Read one comment as a reply to ``task_id`` at ``sha``.

    ⛔There is deliberately no GitHub login check. ChatGPT posts under the same
      login as the bots (``truhojunbot-tech``, estate#1), so the author proves
      nothing. What a bot cannot produce by accident is this header: it must
      name this task and quote the 40-hex sha the request named, and it must
      come after the request on the request's own thread (the caller checks
      that). That is the whole authentication, and it is a limit, not a proof.
    """
    lines = (body or "").strip().splitlines()
    first = lines[0].strip() if lines else ""
    if not first.startswith(REPLY_PREFIX):
        return ReplyParse("foreign")
    header = _HEADER_RE.fullmatch(first)
    if header is None:
        if f"task={task_id}" in first.split():
            return ReplyParse("rejected", reason=f"garbled reply header: {first[:120]!r}")
        return ReplyParse("foreign")
    if header.group(1) != task_id:
        return ReplyParse("foreign")
    if header.group(2) != sha:
        return ReplyParse("rejected", reason=f"sha mismatch: reply names {header.group(2)[:40]}, "
                                             f"request named {sha}")
    rest = lines[1:]
    verdict_lines = [i for i, line in enumerate(rest) if line.strip().upper().startswith("VERDICT")]
    if len(verdict_lines) != 1:
        return ReplyParse("rejected", reason=("missing VERDICT line" if not verdict_lines
                                              else f"{len(verdict_lines)} VERDICT lines"))
    verdict = _VERDICT_RE.fullmatch(rest[verdict_lines[0]].strip())
    if verdict is None:
        return ReplyParse("rejected",
                          reason=f"garbled VERDICT line: {rest[verdict_lines[0]].strip()[:80]!r}")
    text = "\n".join(line for i, line in enumerate(rest) if i != verdict_lines[0]).strip()
    return ReplyParse("accepted", verdict=verdict.group(1), text=text)


def review_result(parsed: ReplyParse, *, task_id: str, sha: str, pr_number: int) -> TaskResult:
    """Map an accepted reply onto the review contract every reviewer uses:
    ``approve`` carries no findings (its text is kept in the summary), and
    ``request_changes`` carries the reply text as its finding."""
    summary = f"ChatGPT review of PR #{pr_number} at {sha}: {parsed.verdict}"
    if parsed.verdict == "approve":
        if parsed.text:
            summary += "\n\n" + parsed.text
        findings: list = []
    else:
        findings = [parsed.text]
    return TaskResult(task_id=task_id, status="completed", verdict=parsed.verdict,  # type: ignore[arg-type]
                      summary=summary[:4000], findings=findings, pr_number=pr_number)


# ---------------------------------------------------------------------------
# Lifecycle.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChatGPTOutcome:
    task_id: str = ""
    #: dispatch: "dispatched" | "no_task" | "carrier_refused" | "self_review"
    #:   | "admission_refused" | "github_unavailable" | "needs_human"
    #: reconcile: "review_completed" | "still_waiting" | "needs_human" | "timed_out"
    action: str = ""
    detail: str = ""


def _notice(queue: TaskQueue, task_id: str, kind: str, detail: str) -> None:
    from agent_crew.server import coordinator_notice_once
    coordinator_notice_once(queue, task_id, kind, detail)


def _end(queue: TaskQueue, task: TaskRequest, status: str, kind: str,
         detail: str) -> ChatGPTOutcome:
    """End a chatgpt review without a verdict: ``needs_human`` or ``timed_out``.

    Through ``TaskQueue.submit_result``, not the server route: a failed review
    there may fall back to another reviewer, and this backend's whole point is
    that ChatGPT is the only reviewer."""
    logger.warning("chatgpt_review: %s ends %s — %s", task.task_id, status, detail)
    queue.patch_context(task.task_id, {"chatgpt_ended_at": time.time(),
                                       "chatgpt_end_reason": detail[:500]})
    queue.submit_result(task.task_id, TaskResult(
        task_id=task.task_id, status=status, summary=f"chatgpt review: {detail}"[:4000],
        error_info={"reason": kind}))  # type: ignore[arg-type]
    if queue.get_attribution(task.task_id) is not None:
        queue.update_attribution_status(task.task_id, status)
        queue.update_attribution_outcome(task.task_id, status)
    _notice(queue, task.task_id, kind, detail)
    return ChatGPTOutcome(task.task_id, status, detail)


def _pinned(task: TaskRequest) -> tuple[Optional[int], str]:
    context = task.context if isinstance(task.context, dict) else {}
    pr = context.get("pr_number") or task.pr_number
    sha = context.get("reviewed_sha")
    if (not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0
            or not isinstance(sha, str) or not _SHA_RE.fullmatch(sha)):
        return None, ""
    return pr, sha


def _claimable(queue: TaskQueue, now: float) -> list[TaskRequest]:
    def claimable(task: TaskRequest) -> bool:
        context = task.context if isinstance(task.context, dict) else {}
        if task.task_type != "review" or context.get("agent_override") not in (None, PROVIDER_NAME):
            return False
        try:
            return float(context.get("push_not_before") or 0) <= now
        except (TypeError, ValueError):
            return False
    return [task for task in queue.list_tasks(status="pending") if claimable(task)]


def _acceptance_text(queue: TaskQueue, task: TaskRequest) -> str:
    """The original implement task's description: walk prev_task_id back to
    the first implement row (a fix round's own description is not the spec)."""
    context = task.context if isinstance(task.context, dict) else {}
    found = ""
    seen: set[str] = set()
    current = context.get("prev_task_id")
    while isinstance(current, str) and current and current not in seen:
        seen.add(current)
        parent = queue.get_task(current)
        if parent is None:
            break
        if parent.task_type == "implement":
            found = parent.description
        parent_context = parent.context if isinstance(parent.context, dict) else {}
        current = parent_context.get("prev_task_id")
    return found[:6000]


def _issue_link(task: TaskRequest, repo: str) -> str:
    context = task.context if isinstance(task.context, dict) else {}
    if context.get("issue_url"):
        return str(context["issue_url"])
    number = context.get("issue_number") or context.get("issue")
    if isinstance(number, int) and not isinstance(number, bool) and number > 0:
        return f"https://github.com/{_github._repo_slug(repo)}/issues/{number}"
    return ""


def dispatch_chatgpt_review(
    queue: TaskQueue, *, role_to_agent: Optional[dict[str, str]] = None,
    review_guard_fn: Optional[Callable[[TaskRequest, str], bool]] = None,
    repo: Optional[str] = None, state_path: Optional[str] = None,
    gh: Optional[GhClient] = None, now: Optional[float] = None,
) -> ChatGPTOutcome:
    """Hand at most one pending review to ChatGPT by posting its request."""
    from agent_crew.claude_cloud import _resolve_task_repo
    gh = gh or GhClient()
    now = time.time() if now is None else now
    role_to_agent = role_to_agent if role_to_agent is not None else {"reviewer": PROVIDER_NAME}
    pending = _claimable(queue, now)
    if not pending:
        return ChatGPTOutcome(action="no_task")
    # The carrier is checked before the claim: a refused dispatch leaves the
    # review pending (it runs once the env is fixed) and says so once per task.
    probe = pending[0]
    probe_pr, _ = _pinned(probe)
    thread, refusal = request_thread(_resolve_task_repo(probe, repo, state_path),
                                     probe_pr or 0)
    if thread is None:
        for task in pending:
            _notice(queue, task.task_id, "chatgpt_review_carrier_refused", refusal)
        return ChatGPTOutcome(action="carrier_refused", detail=refusal)

    task = queue.dequeue(agent=PROVIDER_NAME, role="reviewer", claimed_via="chatgpt_push",
                         skip_deferred=True, role_to_agent=role_to_agent)
    if task is None:
        return ChatGPTOutcome(action="no_task")
    if review_guard_fn is not None and review_guard_fn(task, PROVIDER_NAME):
        return ChatGPTOutcome(task.task_id, "self_review")
    pr_number, sha = _pinned(task)
    if pr_number is None:
        return _end(queue, task, "needs_human", "chatgpt_review_missing_pinned_pr_or_sha",
                    "review has no pinned PR number or 40-hex reviewed_sha")
    task_repo = _resolve_task_repo(task, repo, state_path)
    if not task_repo:
        return _end(queue, task, "needs_human", "chatgpt_review_repo_unresolved",
                    "review repo could not be resolved")
    thread, refusal = request_thread(task_repo, pr_number)
    if thread is None:  # only if the env changed between the probe and the claim
        return _end(queue, task, "needs_human", "chatgpt_review_carrier_refused", refusal)

    pull = gh.pull(task_repo, pr_number)
    files = gh.pull_files(task_repo, pr_number) if pull else None
    base_sha = ((pull or {}).get("base") or {}).get("sha") or ""
    if pull is None or files is None or not _SHA_RE.fullmatch(str(base_sha)):
        # Nothing was posted yet; GitHub being unreadable is not a verdict.
        queue.patch_context(task.task_id, {"push_not_before": now + 60})
        queue.requeue(task.task_id)
        return ChatGPTOutcome(task.task_id, "github_unavailable", "PR read failed; retry in 60s")
    pr_url = (pull.get("html_url")
              or f"https://github.com/{_github._repo_slug(task_repo)}/pull/{pr_number}")
    body = build_request_comment(
        project=task.project or "", task_id=task.task_id, pr_number=pr_number, pr_url=pr_url,
        head_sha=sha, base_sha=base_sha, issue_link=_issue_link(task, task_repo),
        acceptance=_acceptance_text(queue, task), files=files,
        suite_counts=suite_count_lines(pull.get("body") or ""))

    thread_repo, thread_number = thread
    try:
        queue.record_dispatch(
            task.task_id, channel=DISPATCH_CHANNEL, agent=PROVIDER_NAME,
            target=f"{_github._repo_slug(thread_repo)}#{thread_number}",
            lease_owner=f"chatgpt:{task.task_id}",
            # The orphan-lease reaper ends the row timed_out if this
            # reconciler never runs again; it never approves.
            lease_seconds=timeout_seconds() + poll_seconds(), ts=now)
    except AdmissionRefused as exc:
        logger.warning("chatgpt_review: dispatch refused for %s — %s", task.task_id, exc)
        return ChatGPTOutcome(task.task_id, "admission_refused", str(exc))
    queue.record_attribution(
        task_id=task.task_id, project=task.project or "", agent=PROVIDER_NAME,
        role="reviewer", task_type="review", worktree_path="", git_branch=task.branch or "",
        status="in_progress", previous_task_id=str((task.context or {}).get("prev_task_id") or ""),
        started_at=now)
    posted = gh.post_comment(thread_repo, thread_number, body)
    if posted is None:
        # The post may still have landed (a timeout after GitHub accepted it),
        # so a blind re-post could leave two requests. A human decides.
        return _end(queue, task, "needs_human", "chatgpt_review_request_failed",
                    f"could not post the review request on {thread_repo}#{thread_number}")
    queue.patch_context(task.task_id, {
        "chatgpt_thread": {"repo": _github._repo_slug(thread_repo), "number": thread_number},
        "chatgpt_request_comment_id": int(posted["id"]),
        "chatgpt_request_url": posted.get("html_url") or "",
        "chatgpt_requested_at": now,
        "chatgpt_base_sha": base_sha,
        **({} if (task.context or {}).get("repo") else {"repo": task_repo}),
    })
    queue.set_push_at(task.task_id, ts=now, pane_id=f"chatgpt:{posted['id']}")
    logger.info("chatgpt_review: requested %s on %s#%s comment=%s", task.task_id,
                thread_repo, thread_number, posted["id"])
    return ChatGPTOutcome(task.task_id, "dispatched", posted.get("html_url") or "")


def _created_epoch(row: dict) -> Optional[float]:
    """GitHub's ``created_at`` (``2026-11-07T11:44:30Z``) as epoch seconds."""
    raw = row.get("created_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def reconcile_chatgpt_review(
    queue: TaskQueue, task: TaskRequest, *, submit_review_result_fn=None,
    gh: Optional[GhClient] = None, now: Optional[float] = None,
) -> ChatGPTOutcome:
    """Read the request thread at most every ``poll_seconds`` and act on the
    first reply that names this task."""
    gh = gh or GhClient()
    now = time.time() if now is None else now
    context = task.context if isinstance(task.context, dict) else {}
    thread = context.get("chatgpt_thread") or {}
    request_id = context.get("chatgpt_request_comment_id")
    pr_number, sha = _pinned(task)
    if (not isinstance(thread, dict) or not thread.get("repo") or not thread.get("number")
            or not isinstance(request_id, int) or pr_number is None):
        return _end(queue, task, "needs_human", "chatgpt_review_state_missing",
                    "dispatched review has no recorded request thread/comment")
    try:
        requested_at = float(context.get("chatgpt_requested_at") or queue.get_dispatched_at(task.task_id))
    except (TypeError, ValueError):
        requested_at = now
    timed_out = now - requested_at > timeout_seconds()
    last_poll = context.get("chatgpt_last_poll_at")
    if (not timed_out and isinstance(last_poll, (int, float))
            and now - last_poll < poll_seconds()):
        return ChatGPTOutcome(task.task_id, "still_waiting", "poll interval not reached")
    queue.patch_context(task.task_id, {"chatgpt_last_poll_at": now})

    comments = gh.comments(thread["repo"], int(thread["number"]))
    hold = ""
    if comments is None:
        hold = "thread read unavailable"
    else:
        # GitHub comment ids increase monotonically: only comments after the
        # request, on the request's own thread, can answer it.
        # The deadline is judged by when GitHub says a reply was posted, not
        # by when this poll ran: a reply posted in time but read after an
        # outage still counts, and one posted after the deadline never does.
        # A comment whose post time cannot be read is not accepted.
        deadline = requested_at + timeout_seconds()
        later = [row for row in comments if isinstance(row, dict)
                 and isinstance(row.get("id"), int) and row["id"] > request_id
                 and (created := _created_epoch(row)) is not None and created <= deadline]
        for row in later:
            parsed = parse_reply(str(row.get("body") or ""), task_id=task.task_id, sha=sha)
            if parsed.kind == "foreign":
                continue
            link = row.get("html_url") or f"comment {row['id']}"
            if parsed.kind == "rejected":
                return _end(queue, task, "needs_human", "chatgpt_review_reply_rejected",
                            f"{parsed.reason} ({link})")
            result = review_result(parsed, task_id=task.task_id, sha=sha, pr_number=pr_number)
            if parsed.verdict == "request_changes" and len(parsed.text) < 10:
                return _end(queue, task, "needs_human", "chatgpt_review_reply_rejected",
                            f"request_changes without findings ({link})")
            if submit_review_result_fn is None:
                hold = "result handler unavailable"
                break
            try:
                acknowledgement = submit_review_result_fn(task.task_id, result)
            except Exception as exc:
                logger.exception("chatgpt_review: result handler failed for %s", task.task_id)
                return _end(queue, task, "needs_human", "chatgpt_review_handler_failed",
                            f"result handler raised {type(exc).__name__} ({link})")
            if isinstance(acknowledgement, dict) and acknowledgement.get("held"):
                hold = f"result held: {acknowledgement['held']}"
                break
            queue.patch_context(task.task_id, {
                "chatgpt_reply_comment_id": row["id"], "chatgpt_reply_url": row.get("html_url") or "",
                "chatgpt_review_verdict": result.verdict, "chatgpt_ended_at": now})
            return ChatGPTOutcome(task.task_id, "review_completed", result.verdict or "")
    if timed_out:
        return _end(queue, task, "timed_out", "chatgpt_review_timed_out",
                    f"no accepted reply within {int(timeout_seconds())}s"
                    + (f" ({hold})" if hold else ""))
    return ChatGPTOutcome(task.task_id, "still_waiting", hold)


def reconcile_chatgpt_reviews(queue: TaskQueue, *, submit_review_result_fn=None,
                              gh: Optional[GhClient] = None,
                              now: Optional[float] = None) -> List[ChatGPTOutcome]:
    """Reconcile every in_progress chatgpt review. Called from the server's
    existing watchdog tick, like the cloud reconciler -- no loop of its own."""
    outcomes = []
    for task in queue.list_in_progress_by_dispatch_channel(DISPATCH_CHANNEL):
        try:
            outcomes.append(reconcile_chatgpt_review(
                queue, task, submit_review_result_fn=submit_review_result_fn, gh=gh, now=now))
        except Exception:
            logger.exception("chatgpt_review: reconcile failed for %s", task.task_id)
    return outcomes
