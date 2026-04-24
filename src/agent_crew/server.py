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


def _pane_has_task(pane_id: str) -> bool:
    """Return True if the pane's visible text still contains the task marker.

    When the task is sitting unsubmitted in the composer, the marker is visible.
    Once Enter is processed the composer clears and the marker disappears.
    """
    r = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", pane_id],
        capture_output=True, text=True,
    )
    return "=== AGENT_CREW TASK ===" in r.stdout


def _default_push(pane_id: str, text: str) -> None:
    """Send task via tmux bracketed paste, then retry Enter until task is submitted.

    Bracketed-paste mode delivers the entire blob atomically. After paste we
    wait for the TUI to finish consuming it, then send Enter. If the pane still
    shows the task marker (Enter was dropped or arrived too early), we wait and
    retry once.
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

    r3 = subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"], capture_output=True)
    logger.debug(f"send-keys Enter result: rc={r3.returncode}, stderr={r3.stderr[:100] if r3.stderr else 'ok'}")

    # Verify delivery: if task marker still visible the Enter was dropped — retry once.
    time.sleep(0.3)
    if _pane_has_task(pane_id):
        logger.warning(f"PUSH: task marker still visible, retrying Enter for {task_id}")
        time.sleep(0.2)
        r4 = subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"], capture_output=True)
        logger.debug(f"send-keys Enter retry result: rc={r4.returncode}")
    else:
        logger.info(f"PUSH SUCCESS: task_id={task_id} pushed to {pane_id}")


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
) -> FastAPI:
    """
    pane_map: {role: pane_id} — e.g. {"implementer": "%475"}. If None, push is disabled.
    port: the HTTP port the server is listening on (embedded in task push messages so
    agents know where to POST results). Defaults to 0 (messages will say port 0).
    push_fn: injectable for testing.
    """
    state: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state["queue"] = TaskQueue(db_path)
        yield

    app = FastAPI(lifespan=lifespan)

    def q() -> TaskQueue:
        return state["queue"]

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

    def _auto_enqueue_review(impl_task_id: str) -> None:
        """Auto-enqueue a review task when an impl task completes.
        This ensures review is triggered independently of CLI timeout."""
        try:
            # Get the original impl task to extract description and branch
            impl_tasks = [t for t in q().list_tasks() if t.task_id == impl_task_id]
            if not impl_tasks:
                return
            impl_task = impl_tasks[0]

            # Create review task with same description/branch, reference to impl task
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
            # Auto-retry: failed task → auto-enqueue retry task (if under max retries)
            if result.status == "failed":
                logger.info(f"POST /tasks/{task_id}/result: task failed with status=failed, attempting auto-retry")
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
)
