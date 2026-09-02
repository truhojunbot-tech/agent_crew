"""Stage cascade hooks — transport-agnostic (Issue #123).

When a task gets a result submitted, three follow-up flows may fire:

  1. ``auto_enqueue_review``           impl ✓  → enqueue a review task
  2. ``auto_enqueue_test``             review approve → enqueue a test task
  3. ``auto_enqueue_fix``              review request_changes → enqueue a fix
  4. ``auto_fallback_failed_task``     rate-limit ✗ → reroute to next agent

Both transports — HTTP ``submit_result`` and MCP ``submit_result`` — must
trigger these so the pipeline doesn't stall after the first stage when an
agent is on the MCP-only path (#106 cutover prerequisite).

The helpers operate on a ``TaskQueue`` and never touch tmux. Push
notifications (paste-buffer + send-keys) are an HTTP-side concern that
remains in ``server.py``: ``_auto_enqueue_review`` etc. wrap these
functions, run them first, and *then* call ``_try_push_next``.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from typing import Optional

from agent_crew.fallback import (
    default_agent_for_role,
    has_rate_limit_signal,
    load_fallback_chains,
    next_agent,
)
from agent_crew.loop import _resolve_verdict
from agent_crew.notify import notify_telegram
from agent_crew.protocol import GateRequest, TaskRequest, TaskResult
from agent_crew.queue import TaskQueue, _TYPE_TO_ROLE

logger = logging.getLogger(__name__)

MAX_FALLBACK_CHAIN_DEPTH = 3

#: Automated fix rounds allowed per review lineage (#244). The cap is the
#: whole reason this transition is safe to automate: a reviewer that keeps
#: rejecting would otherwise drive review→fix→review forever, burning quota on
#: a disagreement no additional round will settle. After the cap the loop stops
#: and says so on the PR, because the next move is a human's.
DEFAULT_REVIEW_FIX_MAX_ROUNDS = 3
#: Bounds on how much review text is copied into the fix task description.
MAX_EMBEDDED_FINDINGS = 20
MAX_FINDING_CHARS = 1000


#: Marker every automated-exhaustion comment carries, so the cascade can
#: recognise its own prior announcement instead of repeating it (#250).
FIX_EXHAUSTED_MARKER = "[agent_crew] Automated fix rounds exhausted"


def pr_is_actionable(pr_number, *, pr_state_fn=None) -> tuple:
    """``(actionable, state)`` — may the cascade still create work for this PR?

    A round budget bounds ONE lineage; it does not make the work useful. #250
    caught the difference: PR #241 merged at 01:42Z and the cascade kept
    completing reviews and posting "a human needs to decide" for another 13.8
    hours. Nothing produced after the merge could reach the artifact, but it
    still burned provider invocations and polluted the review-outcome metrics.

    Three answers, and the third is the one that needs stating:

      * no `pr_number` — actionable. A task can legitimately precede its PR
        (the first implement → review hop), and "no PR" is not "terminal PR".
      * `open` — actionable, unchanged behaviour.
      * `merged`/`closed`/**`unknown`** — not actionable.

    ⛔`unknown` blocks NEW work on purpose. When GitHub cannot be reached we do
      not know whether the PR is terminal, and #250 asks for deferral over
      speculative work in exactly that case: a skipped cascade is recoverable
      (the result is still persisted, and a human or a later task can resume
      it), whereas work spawned against a merged PR is unrecoverable spend. The
      asymmetry is the whole argument — this is not a general fail-closed rule.
    """
    if not pr_number:
        return (True, "no_pr")
    try:
        if pr_state_fn is not None:
            state = pr_state_fn(int(pr_number))
        else:
            from agent_crew.github import pr_state as _pr_state

            state = _pr_state(int(pr_number))
    except Exception as e:  # noqa: BLE001 — a lookup never breaks a cascade
        logger.warning(f"pr_is_actionable: lookup failed for PR #{pr_number}: {e}")
        return (False, "unknown")
    return (state == "open", state or "unknown")


def _skip_terminal_pr(what: str, task_id: str, pr_number, *, pr_state_fn=None) -> Optional[str]:
    """Shared guard for the cascade entry points.

    Returns the blocking state, or ``None`` when the cascade may proceed.
    """
    actionable, state = pr_is_actionable(pr_number, pr_state_fn=pr_state_fn)
    if actionable:
        return None
    if state == "unknown":
        # ⛔Louder than the terminal case, deliberately. "The PR is merged" is a
        #   correct, permanent stop; "we could not ask GitHub" is a stop that
        #   nothing retries, so it must be visible rather than look like normal
        #   cascade completion. #250 asks for deferral here, not for silence.
        logger.warning(
            f"{what}: could not determine the state of PR #{pr_number} — deferring "
            f"follow-up work for {task_id} rather than spending it on a "
            f"possibly-terminal PR. The task result is recorded; re-run the cascade "
            f"once GitHub is reachable (#250)."
        )
    else:
        logger.info(
            f"{what}: PR #{pr_number} is {state} — not creating follow-up work for "
            f"{task_id}. The task result is still recorded; only the cascade stops (#250)."
        )
    return state


def review_fix_max_rounds() -> int:
    """Cap on automated fix rounds. ``0`` disables the transition entirely.

    Read at call time rather than import time so an operator can change the
    limit (or switch the whole feature off) without restarting the server.
    """
    raw = (os.getenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS") or "").strip()
    if not raw:
        return DEFAULT_REVIEW_FIX_MAX_ROUNDS
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            f"AGENT_CREW_REVIEW_FIX_MAX_ROUNDS={raw!r} is not an integer — "
            f"using default {DEFAULT_REVIEW_FIX_MAX_ROUNDS}"
        )
        return DEFAULT_REVIEW_FIX_MAX_ROUNDS


def fix_task_id(review_task_id: str, fix_round: int) -> str:
    """The task id a given review round's fix MUST have.

    ⛔Derived, not random, and that is the entire idempotency mechanism. A
      result POST can arrive twice — a retried submission, a duplicate delivery,
      an operator re-posting — and the cascade runs again in full each time. A
      random id made every replay a NEW task, so two implementers could be
      handed the same branch and the same findings concurrently (review of
      PR #245).

    Deriving the id puts the guard on the `tasks` PRIMARY KEY, where a race
    cannot slip through it. A "does one already exist?" check cannot do that
    job: two concurrent submissions both read "no" and both insert. Same
    reasoning as the #224 claim ledger, where the GitHub label could not be the
    mutex because `--add-label` is idempotent.

    ⛔The review id goes in VERBATIM, not as a truncated hash. The first
      version used `sha256(review_task_id)[:8]`, and 32 bits collide by
      birthday at roughly 65k reviews — at which point two unrelated reviews
      share a key and the second one's fix is silently skipped as "already
      exists". That is strictly worse than the duplicate this key was added to
      prevent: a duplicate is visible, a dropped fix is not (review of PR #245,
      round 2).

      Verbatim makes the mapping injective rather than merely improbable.
      `int()` renders the round as digits only, so the trailing `-r<digits>` is
      unambiguous and no two (review, round) pairs can produce the same string
      — `"-"` and `"r"` are not digits, so a shorter round cannot be misread as
      part of a longer one. Nothing constrains `task_id` length (TEXT PRIMARY
      KEY), and a longer id that is always correct beats a short one that is
      usually correct. It also reads: `fix-review-4be7401c-r1` says which
      review it came from without a lookup.
    """
    return f"fix-{review_task_id}-r{int(fix_round)}"


def _findings_block(findings: list, review_task_id: str) -> str:
    """Render review findings for the fix task description.

    Bounded — but never silently. A dropped finding is a defect the fix task
    would not know to address, so what was omitted is stated along with where
    to read the rest.
    """
    items = [str(f).strip() for f in (findings or []) if str(f).strip()]
    shown = items[:MAX_EMBEDDED_FINDINGS]
    lines = []
    for f in shown:
        if len(f) > MAX_FINDING_CHARS:
            f = (f[:MAX_FINDING_CHARS]
                 + f" [... truncated; full text in review task {review_task_id}]")
        lines.append(f"- {f}")
    if len(items) > len(shown):
        lines.append(
            f"- [... {len(items) - len(shown)} further findings omitted here — "
            f"read them all via GET /tasks/{review_task_id}]"
        )
    return "\n".join(lines)


def auto_enqueue_fix(
    queue: TaskQueue,
    review_task_id: str,
    *,
    pane_map: Optional[dict] = None,
    server_project: Optional[str] = None,
    comment_fn=None,
    pr_state_fn=None,
    already_announced_fn=None,
) -> Optional[str]:
    """Create the fix task that follows a ``request_changes`` review (#244).

    The cascade had transitions for `implement completed → review` and
    `review approve → test`, but the rejection path just ended. Every
    multi-round review therefore needed an operator to hand-enqueue the fix
    with the findings pasted in — `crew triage --watch` automated the first
    claim and nothing after it.

    Returns the new implement task_id, or ``None`` when no fix is created.
    The ``None`` cases are deliberate and each closes a different way this
    transition could misbehave:

      * the review task itself failed — `_resolve_verdict` maps a crashed or
        timed-out review to `request_changes` so a broken review can never
        silently approve (#100), but that is not a fix request: there are no
        findings, and the failure path already retries or falls back. Spawning
        a fix here would double-handle it AND hand an agent nothing to do;
      * the reviewer requested changes without stating anything actionable;
      * `coordinator_managed` — `crew run` drives its own loop;
      * cross-project, as in `auto_enqueue_review`;
      * the round cap is reached;
      * a fix task for this review round already exists. The transition is
        idempotent per review round: a replayed result POST produces no second
        task, and the caller gets ``None`` because nothing NEW was created.
        Note this holds regardless of the existing task's state — including a
        cancelled one, so an operator's explicit cancel is not quietly undone
        by a duplicate delivery.

    Callers swallow the ``None`` — auto-enqueue must never crash a result
    submission.
    """
    try:
        review_tasks = [t for t in queue.list_tasks() if t.task_id == review_task_id]
        if not review_tasks:
            return None
        review_task = review_tasks[0]

        review_result = queue.get_result(review_task_id)
        if not review_result:
            return None
        if getattr(review_result, "status", None) != "completed":
            logger.info(
                f"auto_enqueue_fix: review {review_task_id} did not complete "
                f"(status={getattr(review_result, 'status', None)!r}) — leaving it "
                f"to the retry/fallback path, not enqueueing a fix"
            )
            return None
        if _resolve_verdict(review_result) != "request_changes":
            return None

        review_ctx = review_task.context if isinstance(review_task.context, dict) else {}
        # Same guard as the other two transitions: `crew run`'s foreground loop
        # enqueues its own follow-ups, and a second one here would race it.
        # Checked here rather than only in the caller so the MCP transport gets
        # the guard too (#123 — both transports run the same cascade).
        if review_ctx.get("coordinator_managed"):
            logger.info(
                f"auto_enqueue_fix: review {review_task_id} is coordinator_managed "
                f"— skipping"
            )
            return None

        review_project = review_task.project
        if review_project and server_project and review_project != server_project:
            logger.warning(
                f"auto_enqueue_fix: skipping cross-project fix — review "
                f"project={review_project!r}, server project={server_project!r}"
            )
            return None

        pr_number = review_result.pr_number or review_ctx.get("pr_number")
        # #250: a terminal PR ends the cascade regardless of the round budget.
        # Checked BEFORE the budget so an exhausted lineage on a merged PR stays
        # silent instead of announcing itself to an already-decided artifact.
        if _skip_terminal_pr("auto_enqueue_fix", review_task_id, pr_number,
                             pr_state_fn=pr_state_fn):
            return None
        max_rounds = review_fix_max_rounds()
        # The lineage counter rides in the task context, so it survives a
        # server restart and counts ROUNDS rather than tasks. An in-memory
        # per-task_id counter (the transient-retry shape) could not work here:
        # every round mints new task ids, so it would always read zero.
        fix_round = int(review_ctx.get("fix_round") or 0) + 1
        if max_rounds <= 0 or fix_round > max_rounds:
            logger.warning(
                f"auto_enqueue_fix: review {review_task_id} requested changes but "
                f"the automated fix budget is spent (round {fix_round} > "
                f"max {max_rounds}) — stopping, this needs a human"
            )
            _announce_fix_budget_exhausted(
                pr_number=pr_number, review_task_id=review_task_id,
                max_rounds=max_rounds, findings=review_result.findings or [],
                comment_fn=comment_fn, already_announced_fn=already_announced_fn)
            return None

        findings_text = _findings_block(review_result.findings or [], review_task_id)
        summary = (review_result.summary or "").strip()
        if not findings_text and not summary:
            logger.warning(
                f"auto_enqueue_fix: review {review_task_id} requested changes with "
                f"neither findings nor a summary — nothing to act on, skipping"
            )
            return None

        where = (f"PR #{pr_number}" if pr_number
                 else f"branch {review_task.branch!r}")
        parts = [
            f"Fix {where} per {review_task_id} request_changes "
            f"(automated fix round {fix_round}/{max_rounds}).",
        ]
        if summary:
            parts.append(f"\nReviewer summary: {summary}")
        if findings_text:
            parts.append(f"\nFindings to address:\n{findings_text}")
        parts.append(
            f"\nCommit to the SAME branch {review_task.branch!r} — do not open a "
            f"new PR. Reproduce each finding before fixing it, and say so if one "
            f"does not reproduce."
        )

        fix_context: dict = {
            "prev_task_id": review_task_id,
            "fix_round": fix_round,
            "review_findings": list(review_result.findings or []),
        }
        if pr_number is not None:
            # #186: lets the dispatcher check out the PR head for this task.
            fix_context["pr_number"] = pr_number
        for key in ("no_tester", "issue", "issue_title", "issue_body",
                    "issue_url", "repo"):
            if review_ctx.get(key) is not None:
                fix_context[key] = review_ctx[key]
        implementer_agent = (
            review_ctx.get("implementer_agent")
            or (default_agent_for_role("implementer", pane_map) if pane_map else None)
        )
        if implementer_agent:
            fix_context["implementer_agent"] = implementer_agent

        fix_id = fix_task_id(review_task_id, fix_round)
        # Cheap early-out with a legible log. It is NOT the guard — the
        # `enqueue` below is, because only the PRIMARY KEY is atomic.
        existing = next((t for t in queue.list_tasks() if t.task_id == fix_id), None)
        if existing is not None:
            logger.info(
                f"auto_enqueue_fix: {fix_id} already exists for {review_task_id} "
                f"(status={existing.status!r}) — this review round already has "
                f"its fix task, not enqueueing another"
            )
            return None
        try:
            queue.enqueue(TaskRequest(
                task_id=fix_id,
                task_type="implement",  # type: ignore[arg-type]
                description="\n".join(parts),
                branch=review_task.branch,
                context=fix_context,
                project=review_project,
            ))
        except sqlite3.IntegrityError:
            # A concurrent submission won the insert. That is the mechanism
            # working, not an error: exactly one fix task exists.
            logger.info(
                f"auto_enqueue_fix: {fix_id} was created concurrently for "
                f"{review_task_id} — leaving the winner in place"
            )
            return None
        logger.info(
            f"auto_enqueue_fix: enqueued {fix_id} for {review_task_id} "
            f"(round {fix_round}/{max_rounds})"
        )
        return fix_id
    except Exception as e:
        logger.warning(f"auto_enqueue_fix: unexpected error: {e}")
        return None


def _announce_fix_budget_exhausted(*, pr_number, review_task_id: str,
                                   max_rounds: int, findings: list,
                                   comment_fn=None, already_announced_fn=None) -> None:
    """Say on the PR that automation has stopped. Best-effort, never raises.

    ⛔A silent stop is the worst outcome available here: the PR would simply go
      quiet after a rejection and look like it was still being worked on.

    ⛔Said ONCE per PR. #250 found 25 of these on PR #241, seven inside three
      minutes: every late or duplicate review result whose lineage was already
      over budget announced the same exhaustion again. The message is about the
      PR, not about the individual review task, so repeating it adds nothing and
      buries the one that mattered. When we cannot check (GitHub unreachable) we
      post — a missing escalation is worse than a duplicate one, and unlike
      spawning work a comment costs no provider invocation.
    """
    if not pr_number:
        return
    if already_announced_fn is not None:
        seen = already_announced_fn(int(pr_number), FIX_EXHAUSTED_MARKER)
    else:
        try:
            from agent_crew.github import pr_has_comment_containing

            seen = pr_has_comment_containing(int(pr_number), FIX_EXHAUSTED_MARKER)
        except Exception:  # noqa: BLE001
            seen = None
    if seen is True:
        logger.info(
            f"auto_enqueue_fix: PR #{pr_number} already carries an exhaustion notice "
            f"— not repeating it for {review_task_id} (#250)"
        )
        return
    body = (
        f"{FIX_EXHAUSTED_MARKER} "
        f"(AGENT_CREW_REVIEW_FIX_MAX_ROUNDS={max_rounds}).\n\n"
        f"The reviewer still requests changes after {max_rounds} automated "
        f"round(s), so the loop has stopped rather than spend another one. "
        f"Latest review task: `{review_task_id}`.\n\n"
        + ("**Outstanding findings:**\n"
           + "\n".join(f"- {str(f)[:MAX_FINDING_CHARS]}" for f in findings[:MAX_EMBEDDED_FINDINGS])
           if findings else "")
        + "\n\nA human needs to decide the next move."
    )
    try:
        if comment_fn is not None:
            comment_fn(int(pr_number), body)
            return
        from agent_crew.github import post_pr_comment

        post_pr_comment(int(pr_number), body)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"auto_enqueue_fix: could not comment on PR #{pr_number}: {e}")


def auto_enqueue_review(
    queue: TaskQueue,
    impl_task_id: str,
    pr_number: Optional[int] = None,
    *,
    pane_map: Optional[dict] = None,
    server_project: Optional[str] = None,
    pr_state_fn=None,
) -> Optional[str]:
    """Create the review task that follows a completed impl task.

    Returns the new review task_id, or ``None`` when no review is created
    (cross-project guard, missing impl task, exception). Callers swallow
    the None — auto-enqueue must never crash a result submission.
    """
    try:
        impl_tasks = [t for t in queue.list_tasks() if t.task_id == impl_task_id]
        if not impl_tasks:
            return None
        impl_task = impl_tasks[0]
        impl_ctx = impl_task.context if isinstance(impl_task.context, dict) else {}

        # ⛔The PR this task is about is not necessarily the one the RESULT
        #   names. Both transports pass `result.pr_number`, and an agent may
        #   simply omit it — while the task context has carried the PR since it
        #   was created. Gating on the argument alone therefore read "no PR"
        #   for a task whose PR was merged, and cheerfully queued a review of
        #   a closed artifact (review of PR #251). Resolve the effective PR
        #   once, and use that everywhere below: the gate, the freshness
        #   directive, and the review context the next hop is gated on.
        if pr_number is None:
            ctx_pr = impl_ctx.get("pr_number")
            if isinstance(ctx_pr, int) or (isinstance(ctx_pr, str) and ctx_pr.isdigit()):
                pr_number = int(ctx_pr)
                logger.info(
                    f"auto_enqueue_review: {impl_task_id} reported no pr_number; "
                    f"using #{pr_number} from the task context"
                )

        # #250: reviewing a merged/closed PR cannot change the artifact.
        if _skip_terminal_pr("auto_enqueue_review", impl_task_id, pr_number,
                             pr_state_fn=pr_state_fn):
            return None

        # #161: no-PR guard — if the impl task has neither a branch nor a
        # pr_number, there is nothing for the reviewer to locate. Retrying
        # will also fail, creating an unbounded loop. Skip auto-review
        # and log so the operator can investigate.
        if not impl_task.branch and pr_number is None:
            logger.warning(
                f"auto_enqueue_review: skipping — impl task {impl_task_id} has "
                f"no branch and no pr_number; reviewer has nothing to find (#161)"
            )
            return None

        # Cross-project guard: if the impl task carries a top-level project tag
        # and the server was started for a different project, skip auto-review
        # to prevent misrouting tasks across project queues.
        impl_project = impl_task.project
        if impl_project and server_project and impl_project != server_project:
            logger.warning(
                f"auto_enqueue_review: skipping cross-project review — "
                f"impl project={impl_project!r}, server project={server_project!r}"
            )
            return None

        # Build a freshness directive that is unambiguous about reviewing
        # the live PR HEAD, not a stale local copy or an earlier round.
        if pr_number is not None:
            pr_directive = (
                f"\n\nFRESHNESS: review PR #{pr_number} at its CURRENT head. "
                f"Run `gh pr diff {pr_number}` (and/or "
                f"`gh pr view {pr_number} --json commits`) FIRST. Do NOT "
                f"reuse line numbers from any earlier review round — they "
                f"reference the prior commit. Pin every finding to the "
                f"latest commit's file:line."
            )
        else:
            pr_directive = (
                f"\n\nFRESHNESS: identify the PR for branch "
                f"{impl_task.branch!r} via `gh pr list --head "
                f"{impl_task.branch}`, then `gh pr diff <num>` to fetch "
                f"the live head before pinning findings. Do NOT review "
                f"from a stale local copy."
            )

        # Identify which agent actually implemented this task. Prefer the
        # explicit override (set by upstream fallback), else fall back to
        # the role's default mapping. Recorded so the rate-limit fallback
        # handler can skip it during reviewer selection (#117 — self-review
        # prevention).
        implementer_agent = (
            impl_ctx.get("agent_override")
            or (default_agent_for_role("implementer", pane_map) if pane_map else None)
        )

        review_context = {
            "checklist_layers": ["test_quality", "code_quality", "business_gap"],
            "reviewer_rejects_happy_path_only": True,
            "instructions": (
                "3-layer review: "
                "1) test_quality — coverage, edge cases, mocks; "
                "2) code_quality — naming, error handling, SOLID; "
                "3) business_gap — requirements met, logging, observability."
                + pr_directive
            ),
            "prev_task_id": impl_task_id,
            "pr_number": pr_number,
        }
        if implementer_agent:
            review_context["implementer_agent"] = implementer_agent
        if impl_ctx.get("no_tester"):
            review_context["no_tester"] = True
        # #244: carry the fix-round counter along the lineage. Without this the
        # counter resets every time a fix task produces a fresh review, and the
        # cap that makes review→fix safe to automate would never be reached.
        for key in ("fix_round", "issue", "issue_title", "issue_body",
                    "issue_url", "repo"):
            if impl_ctx.get(key) is not None:
                review_context[key] = impl_ctx[key]

        # #164: compact review description — avoid re-injecting the full
        # original spec into the reviewer's context. The reviewer should
        # use get_task(prev_task_id) when the full spec is needed.
        if pr_number is not None:
            compact_desc = f"Review PR #{pr_number} for task {impl_task_id}."
        else:
            compact_desc = (
                f"Review branch {impl_task.branch!r} for task {impl_task_id}."
            )

        review_id = f"review-{uuid.uuid4().hex[:8]}"
        review_req = TaskRequest(
            task_id=review_id,
            task_type="review",  # type: ignore[arg-type]
            description=compact_desc,
            branch=impl_task.branch,
            context=review_context,
            project=impl_project,
        )
        queue.enqueue(review_req)
        return review_id
    except Exception as e:
        logger.warning(f"auto_enqueue_review: unexpected error: {e}")
        return None


def auto_enqueue_test(
    queue: TaskQueue,
    review_task_id: str,
    *,
    pane_map: Optional[dict] = None,
    pr_state_fn=None,
) -> Optional[str]:
    """Create the test task that follows an approved review.

    Returns the new test task_id, or ``None`` when no test is created
    (review missing/rejected, exception).
    """
    try:
        review_tasks = [t for t in queue.list_tasks() if t.task_id == review_task_id]
        if not review_tasks:
            return None
        review_task = review_tasks[0]

        # Use the defensive verdict resolver from loop.py so reviewers that
        # post `verdict=null` with empty findings still trip the auto-test
        # (#100).
        review_result = queue.get_result(review_task_id)
        if not review_result:
            return None
        if _resolve_verdict(review_result) != "approve":
            return None

        # Propagate upstream agent identities so review/test fallback can
        # avoid self-review and self-test (#117).
        review_ctx = review_task.context if isinstance(review_task.context, dict) else {}
        implementer_agent = review_ctx.get("implementer_agent")
        reviewer_agent = (
            review_ctx.get("agent_override")
            or (default_agent_for_role("reviewer", pane_map) if pane_map else None)
        )

        pr_number = review_ctx.get("pr_number")
        # #250: same gate — a merged PR does not need testing on our account.
        if _skip_terminal_pr("auto_enqueue_test", review_task_id, pr_number,
                             pr_state_fn=pr_state_fn):
            return None
        test_context: dict = {"prev_task_id": review_task_id}
        if pr_number is not None:
            test_context["pr_number"] = pr_number  # #171: propagate for post-test merge
        if implementer_agent:
            test_context["implementer_agent"] = implementer_agent
        if reviewer_agent:
            test_context["reviewer_agent"] = reviewer_agent

        # #164: compact test description — reviewer can fetch full spec via
        # get_task(prev_task_id) chain if needed.
        if pr_number is not None:
            compact_desc = f"Test PR #{pr_number} for reviewed task {review_task_id}."
        else:
            compact_desc = (
                f"Test branch {review_task.branch!r} for reviewed task {review_task_id}."
            )

        test_id = f"test-{uuid.uuid4().hex[:8]}"
        test_req = TaskRequest(
            task_id=test_id,
            task_type="test",  # type: ignore[arg-type]
            description=compact_desc,
            branch=review_task.branch,
            context=test_context,
        )
        queue.enqueue(test_req)
        return test_id
    except Exception as e:
        logger.warning(f"auto_enqueue_test: unexpected error: {e}")
        return None


def auto_fallback_failed_task(
    queue: TaskQueue,
    task_id: str,
    result: TaskResult,
    task_type: str,
    *,
    pane_map: Optional[dict] = None,
    state_path: Optional[str] = None,
    fallback_disabled: bool = False,
) -> bool:
    """Reroute a rate-limit-shaped failure to the next agent in the chain.

    Returns ``True`` when fallback handled the task — caller should skip
    auto-retry. ``False`` means caller should fall through to its normal
    retry path. On chain exhaustion, opens an ``escalation`` gate and
    sends a Telegram alert (best-effort).
    """
    if fallback_disabled:
        return False
    if not has_rate_limit_signal(result.summary, result.findings):
        return False

    try:
        tasks = [t for t in queue.list_tasks() if t.task_id == task_id]
        if not tasks:
            return False
        original = tasks[0]
        ctx = dict(original.context) if isinstance(original.context, dict) else {}

        # #167: stop infinite fallback loops — if the chain has already been
        # retried MAX_FALLBACK_CHAIN_DEPTH times, cancel the original task and
        # escalate without creating another fallback task.
        if ctx.get("fallback_chain_depth", 0) >= MAX_FALLBACK_CHAIN_DEPTH:
            logger.warning(
                f"auto_fallback: fallback_chain_depth={ctx.get('fallback_chain_depth')} "
                f">= MAX ({MAX_FALLBACK_CHAIN_DEPTH}) for {task_id} — cancelling chain."
            )
            # Cancel the original root task so the chain has a definitive
            # terminal state of "cancelled" (not "failed") in the DB.
            original_task_id = ctx.get("original_task_id")
            if original_task_id:
                try:
                    queue.cancel(original_task_id)
                    logger.info(
                        f"auto_fallback: cancelled original task {original_task_id} "
                        f"due to fallback loop detection"
                    )
                except Exception as e:
                    logger.warning(
                        f"auto_fallback: failed to cancel original task {original_task_id}: {e}"
                    )
            msg = (
                f"agent_crew fallback loop detected\n"
                f"task_id: {task_id}\n"
                f"task_type: {task_type}\n"
                f"chain_depth: {ctx.get('fallback_chain_depth')}\n"
                f"original_task_id: {original_task_id or '(unknown)'}\n"
                f"last summary: {(result.summary or '')[:200]}"
            )
            try:
                queue.create_gate(
                    GateRequest(
                        id=f"escalation-{task_id}-{uuid.uuid4().hex[:4]}",
                        type="escalation",
                        message=msg,
                        status="pending",
                    )
                )
            except Exception as e:
                logger.warning(f"auto_fallback: failed to create escalation gate: {e}")
            try:
                notify_telegram(msg)
            except Exception:
                pass
            return True

        role = _TYPE_TO_ROLE.get(task_type)
        current_agent = (
            ctx.get("agent_override")
            or (default_agent_for_role(role, pane_map) if (role and pane_map) else None)
        )
        excluded = list(ctx.get("fallback_excluded") or [])
        # Self-review/self-test prevention (#117): any upstream agent
        # already in the lineage (impl→review→test) must be excluded so
        # the chain doesn't loop the task back to a participant whose
        # output is being judged.
        for upstream_key in ("implementer_agent", "reviewer_agent"):
            upstream = ctx.get(upstream_key)
            if upstream and upstream not in excluded:
                excluded.append(upstream)
        if current_agent and current_agent not in excluded:
            excluded.append(current_agent)

        chains = load_fallback_chains(state_path)
        successor = next_agent(task_type, current_agent, excluded, chains)

        if successor is None:
            logger.warning(
                f"auto_fallback: chain exhausted for {task_id} "
                f"(task_type={task_type}, excluded={excluded}). Escalating."
            )
            msg = (
                f"agent_crew rate-limit fallback exhausted\n"
                f"task_id: {task_id}\n"
                f"task_type: {task_type}\n"
                f"tried agents: {', '.join(excluded) or '(none)'}\n"
                f"last summary: {(result.summary or '')[:200]}"
            )
            try:
                queue.create_gate(
                    GateRequest(
                        id=f"escalation-{task_id}-{uuid.uuid4().hex[:4]}",
                        type="escalation",
                        message=msg,
                        status="pending",
                    )
                )
            except Exception as e:
                logger.warning(f"auto_fallback: failed to create escalation gate: {e}")
            try:
                notify_telegram(msg)
            except Exception:
                pass
            return True

        new_ctx = dict(ctx)
        new_ctx["agent_override"] = successor
        new_ctx["fallback_excluded"] = excluded
        new_ctx["fallback_from_task_id"] = task_id
        new_ctx["fallback_chain_depth"] = ctx.get("fallback_chain_depth", 0) + 1
        # Carry the root task_id through the chain so loop detection can
        # cancel the original task when the depth limit is reached (#167).
        new_ctx["original_task_id"] = ctx.get("original_task_id") or task_id
        try:
            fallback_req = TaskRequest(
                task_id=f"fallback-{task_id}-{uuid.uuid4().hex[:4]}",
                task_type=task_type,  # type: ignore[arg-type]
                description=original.description,
                branch=original.branch,
                priority=original.priority,
                context=new_ctx,
            )
            queue.enqueue(fallback_req)
            logger.info(
                f"auto_fallback: rerouted {task_id} -> {successor} "
                f"(excluded={excluded})"
            )
            return True
        except Exception as e:
            logger.warning(f"auto_fallback: enqueue failed for {task_id}: {e}")
            return False
    except Exception as e:
        logger.warning(f"auto_fallback: unexpected error for {task_id}: {e}")
        return False
