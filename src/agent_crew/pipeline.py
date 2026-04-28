"""Stage cascade hooks — transport-agnostic (Issue #123).

When a task gets a result submitted, three follow-up flows may fire:

  1. ``auto_enqueue_review``           impl ✓  → enqueue a review task
  2. ``auto_enqueue_test``             review approve → enqueue a test task
  3. ``auto_fallback_failed_task``     rate-limit ✗ → reroute to next agent

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


def auto_enqueue_review(
    queue: TaskQueue,
    impl_task_id: str,
    pr_number: Optional[int] = None,
    *,
    pane_map: Optional[dict] = None,
    server_project: Optional[str] = None,
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
        impl_ctx = impl_task.context if isinstance(impl_task.context, dict) else {}
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

        review_id = f"review-{uuid.uuid4().hex[:8]}"
        review_req = TaskRequest(
            task_id=review_id,
            task_type="review",  # type: ignore[arg-type]
            description=impl_task.description,
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

        test_context = {"prev_task_id": review_task_id}
        if implementer_agent:
            test_context["implementer_agent"] = implementer_agent
        if reviewer_agent:
            test_context["reviewer_agent"] = reviewer_agent

        test_id = f"test-{uuid.uuid4().hex[:8]}"
        test_req = TaskRequest(
            task_id=test_id,
            task_type="test",  # type: ignore[arg-type]
            description=review_task.description,
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
