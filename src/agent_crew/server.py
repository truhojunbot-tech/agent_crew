import asyncio
import contextlib
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Callable, Literal, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from agent_crew.anomaly import check_wrong_repo
from agent_crew.fallback import (
    default_agent_for_role,
    has_rate_limit_signal,
    load_fallback_chains,
    next_agent,
)
from agent_crew.notify import notify_telegram
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


def _pane_is_busy(pane_id: str) -> bool:
    """Return True if the pane content changed since the previous call.

    Pure content diff. Independent of any per-CLI banner text — Claude Code,
    codex, and gemini all change `tmux capture-pane -p` output as work
    progresses (token counters tick, spinner glyphs animate, output streams
    in). A pane that is genuinely idle produces an identical capture across
    consecutive calls.

    Earlier versions matched on banner strings (``✻``, ``Crunched``,
    ``Working``, ``Thinking``, ``esc to interrupt``). Both approaches are
    brittle: glyph forms persist as past-tense scrollback (the original #84
    failure mode) and the canonical ``esc to interrupt`` is one Claude UI
    refresh away from being renamed. Comparing whole captures sidesteps the
    text dependency entirely.

    Edge cases:
    - First call for a pane has no prior snapshot → returns False (idle).
      That's safe because activity is freshly bumped at task enqueue, so the
      idle clock is well below any threshold.
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
    return prev is not None and current != prev


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


def _format_reminder_message(task_id: str, port: int, idle_seconds: float) -> str:
    """Watchdog nudge: agent has been silent past the heartbeat threshold.

    Includes the canonical POST template so the agent can resolve the task in
    one paste even if the original block scrolled out of context. Also covers
    the API stream-timeout recovery path (issue #85): after a transient
    network failure the agent CLI may drop back to the prompt with no result
    posted; this reminder is the canonical handoff to either retry or fail
    out cleanly.
    """
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


def _format_task_message(task: TaskRequest, port: int) -> str:
    ctx = json.dumps(task.context, ensure_ascii=False)
    return (
        f"=== AGENT_CREW TASK ===\n"
        f"task_id: {task.task_id}\n"
        f"task_type: {task.task_type}\n"
        f"branch: {task.branch}\n"
        f"priority: {task.priority}\n"
        f"context: {ctx}\n"
        f"description: {task.description}\n"
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
    watchdog_interval: Optional[float] = None,
    reminder_seconds: Optional[float] = None,
    timeout_seconds: Optional[float] = None,
    watchdog_disabled: Optional[bool] = None,
    anomaly_interval: Optional[float] = None,
    anomaly_disabled: Optional[bool] = None,
    state_path: Optional[str] = None,
    fallback_disabled: Optional[bool] = None,
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
    """
    if watchdog_interval is None:
        watchdog_interval = float(os.getenv("AGENT_CREW_WATCHDOG_INTERVAL", "30"))
    if reminder_seconds is None:
        reminder_seconds = float(os.getenv("AGENT_CREW_REMINDER_SECONDS", "300"))
    if timeout_seconds is None:
        timeout_seconds = float(os.getenv("AGENT_CREW_TIMEOUT_SECONDS", "900"))
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

    state: dict = {}
    reminded_task_ids: set[str] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state["queue"] = TaskQueue(db_path)
        background_tasks: list[asyncio.Task] = []
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

        logger.info(f"_try_push_next: dequeued task_id={task.task_id}, calling push_fn")
        push_fn(pane_id, _format_task_message(task, port))

    def _try_push_discuss(agent: Optional[str]) -> None:
        """Discuss tasks fan out per agent, not per role. pane_map is expected
        to hold agent-name keys (e.g. 'claude', 'codex', 'gemini') alongside
        the role keys. Busy-check and dequeue are both scoped to the agent so
        concurrent panelists don't block each other."""
        logger.debug(f"_try_push_discuss: agent={agent}")
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
                if pane_busy_fn(pane_id):
                    q().bump_activity(task_id, ts=now)
                    actions["bumped"].append(task_id)
                    # Busy pane resets the reminder cycle — agent is alive.
                    reminded_task_ids.discard(task_id)
                    continue
            except Exception:
                logger.exception(f"watchdog: pane_busy_fn raised for {pane_id}")
                continue

            idle_for = now - (row["last_activity_at"] or now)
            if idle_for >= timeout_seconds:
                summary = (
                    f"watchdog timeout: pane idle {idle_for:.0f}s without "
                    f"sign of activity (threshold {timeout_seconds:.0f}s)"
                )
                tt = q().force_fail(task_id, summary)
                logger.error(
                    f"WATCHDOG TIMEOUT: task_id={task_id} marked failed; "
                    f"task_type={tt}, idle_for={idle_for:.0f}s"
                )
                reminded_task_ids.discard(task_id)
                actions["timed_out"].append(task_id)
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
                try:
                    push_fn(pane_id, _format_reminder_message(task_id, port, idle_for))
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

    def _auto_enqueue_review(impl_task_id: str) -> None:
        """Auto-enqueue a review task when an impl task completes.
        This ensures review is triggered independently of CLI timeout."""
        try:
            # Get the original impl task to extract description and branch
            impl_tasks = [t for t in q().list_tasks() if t.task_id == impl_task_id]
            if not impl_tasks:
                return
            impl_task = impl_tasks[0]

            # Cross-project guard: if the impl task carries a top-level project tag
            # and the server was started for a different project, skip auto-review to
            # prevent misrouting tasks across project queues.
            impl_project = impl_task.project  # top-level field, typed
            if impl_project and project and impl_project != project:
                logger.warning(
                    f"_auto_enqueue_review: skipping cross-project review — "
                    f"impl project={impl_project!r}, server project={project!r}"
                )
                return

            # Create review task with same description/branch, reference to impl task.
            # Inherit top-level project from impl task so the cross-project guard works
            # for subsequent hops in the pipeline (review → test).
            review_context = {
                "checklist_layers": ["test_quality", "code_quality", "business_gap"],
                "reviewer_rejects_happy_path_only": True,
                "instructions": (
                    "3-layer review: "
                    "1) test_quality — coverage, edge cases, mocks; "
                    "2) code_quality — naming, error handling, SOLID; "
                    "3) business_gap — requirements met, logging, observability."
                ),
                "prev_task_id": impl_task_id,
            }
            review_req = TaskRequest(
                task_id=f"review-{uuid.uuid4().hex[:8]}",
                task_type="review",
                description=impl_task.description,
                branch=impl_task.branch,
                context=review_context,
                project=impl_project,  # top-level field propagated
            )
            q().enqueue(review_req)
            # Try to push the newly enqueued review task to reviewer pane
            _try_push_next("reviewer")
        except Exception:
            # Silently fail auto-enqueue — don't crash the result submission
            pass

    def _auto_enqueue_test(review_task_id: str) -> None:
        """Auto-enqueue a test task when a review task is approved.
        This ensures testing is triggered independently of CLI timeout."""
        try:
            # Get the review task to extract description and branch
            review_tasks = [t for t in q().list_tasks() if t.task_id == review_task_id]
            if not review_tasks:
                return
            review_task = review_tasks[0]

            # Get the review result to confirm it was approved
            review_result = q().get_result(review_task_id)
            if not review_result or review_result.verdict != "approve":
                return

            # Create test task with same description/branch, reference to review task
            test_context = {
                "prev_task_id": review_task_id,
            }
            test_req = TaskRequest(
                task_id=f"test-{uuid.uuid4().hex[:8]}",
                task_type="test",
                description=review_task.description,
                branch=review_task.branch,
                context=test_context,
            )
            q().enqueue(test_req)
            # Try to push the newly enqueued test task to tester pane
            _try_push_next("tester")
        except Exception:
            # Silently fail auto-enqueue — don't crash the result submission
            pass

    _MAX_RETRIES = 2  # Maximum retry attempts per task

    def _auto_retry_failed_task(task_id: str, result: TaskResult, task_type: str) -> None:
        """Auto-retry a failed task if it hasn't exceeded max retries.
        This provides resilience against transient failures."""
        MAX_RETRIES = 2
        if result.retry_count >= MAX_RETRIES:
            logger.info(f"Task {task_id} failed with status={result.status}, but max retries ({MAX_RETRIES}) reached")
            return
        try:
            # Get the original task to extract description, branch, and context
            tasks = [t for t in q().list_tasks() if t.task_id == task_id]
            if not tasks:
                return
            original_task = tasks[0]

            # Create retry task with incremented retry count
            retry_context = dict(original_task.context) if isinstance(original_task.context, dict) else {}
            retry_context["retry_attempt"] = (result.retry_count or 0) + 1
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

    def _auto_fallback_failed_task(
        task_id: str,
        result: TaskResult,
        task_type: str,
    ) -> bool:
        """If the failure is rate-limit-shaped, reroute to the next agent in
        the fallback chain. Returns True when fallback handled the task
        (caller should skip auto_retry); False when caller should fall through
        to the normal retry path.

        On chain exhaustion, opens an ``escalation`` gate and tries to send a
        Telegram alert via the #79 helper (best-effort).
        """
        if fallback_disabled:
            return False
        if not has_rate_limit_signal(result.summary, result.findings):
            return False

        try:
            tasks = [t for t in q().list_tasks() if t.task_id == task_id]
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
            if current_agent and current_agent not in excluded:
                excluded.append(current_agent)

            chains = load_fallback_chains(state_path)
            successor = next_agent(task_type, current_agent, excluded, chains)

            if successor is None:
                # Chain exhausted — escalate.
                logger.warning(
                    f"_auto_fallback: chain exhausted for {task_id} "
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
                    q().create_gate(
                        GateRequest(
                            id=f"escalation-{task_id}-{uuid.uuid4().hex[:4]}",
                            type="escalation",
                            message=msg,
                            status="pending",
                        )
                    )
                except Exception as e:
                    logger.warning(f"_auto_fallback: failed to create escalation gate: {e}")
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
                    task_type=task_type,  # type: ignore
                    description=original.description,
                    branch=original.branch,
                    priority=original.priority,
                    context=new_ctx,
                )
                q().enqueue(fallback_req)
                logger.info(
                    f"_auto_fallback: rerouted {task_id} -> {successor} "
                    f"(excluded={excluded})"
                )
                if role:
                    _try_push_next(role)
                return True
            except Exception as e:
                logger.warning(f"_auto_fallback: enqueue failed for {task_id}: {e}")
                return False
        except Exception as e:
            logger.warning(f"_auto_fallback: unexpected error for {task_id}: {e}")
            return False

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/tasks", status_code=201)
    def post_task(task: TaskRequest):
        logger.info(f"POST /tasks: task_type={task.task_type}, task_id (will assign)...")
        task_id = q().enqueue(task)
        logger.info(f"POST /tasks: enqueued task_id={task_id}")
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
            # Auto-transition: impl task completed → auto-enqueue review task
            if task_type == "implement" and result.status == "completed":
                logger.info(f"POST /tasks/{task_id}/result: impl task completed, auto-enqueueing review")
                _auto_enqueue_review(task_id)
            # Auto-transition: review task approved → auto-enqueue test task
            if task_type == "review" and result.verdict == "approve":
                logger.info(f"POST /tasks/{task_id}/result: review task approved, auto-enqueueing test")
                _auto_enqueue_test(task_id)
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
    db_path=os.path.expanduser(os.getenv("AGENT_CREW_DB", "/tmp/agent_crew_default.db")),
    pane_map=_load_pane_map(),
    port=int(os.getenv("AGENT_CREW_PORT", "0") or 0),
    state_path=os.path.expanduser(os.getenv("AGENT_CREW_STATE", "")) or None,
)
