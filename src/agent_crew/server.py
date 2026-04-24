import json
import os
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from typing import Callable, Literal, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from agent_crew.protocol import GateRequest, TaskRequest, TaskResult
from agent_crew.queue import TaskQueue, _ROLE_TO_TYPE, _TYPE_TO_ROLE


class ResolveBody(BaseModel):
    status: Literal["approved", "rejected"]


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
    subprocess.run(
        ["tmux", "load-buffer", "-"],
        input=text,
        text=True,
        capture_output=True,
    )
    subprocess.run(
        ["tmux", "paste-buffer", "-p", "-d", "-t", pane_id],
        capture_output=True,
    )
    # Give the TUI time to process the bracketed-paste sequence.
    time.sleep(0.5)
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"], capture_output=True)
    # Verify delivery: if task marker still visible the Enter was dropped — retry once.
    time.sleep(0.3)
    if _pane_has_task(pane_id):
        time.sleep(0.2)
        subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"], capture_output=True)


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
        if not pane_map:
            return
        pane_id = pane_map.get(role)
        if not pane_id:
            return
        task_type = _ROLE_TO_TYPE.get(role)
        if task_type is None:
            return
        if q().has_in_progress(task_type):
            return  # agent busy; will get pushed when current task completes
        task = q().dequeue(role=role)
        if task is None:
            return  # nothing pending
        push_fn(pane_id, _format_task_message(task, port))

    def _try_push_discuss(agent: Optional[str]) -> None:
        """Discuss tasks fan out per agent, not per role. pane_map is expected
        to hold agent-name keys (e.g. 'claude', 'codex', 'gemini') alongside
        the role keys. Busy-check and dequeue are both scoped to the agent so
        concurrent panelists don't block each other."""
        if not pane_map or not agent:
            return
        pane_id = pane_map.get(agent)
        if not pane_id:
            return
        if q().has_discuss_in_progress_for_agent(agent):
            return
        task = q().dequeue_discuss_for_agent(agent)
        if task is None:
            return
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

    @app.post("/tasks", status_code=201)
    def post_task(task: TaskRequest):
        task_id = q().enqueue(task)
        if task.task_type == "discuss":
            agent = task.context.get("agent") if isinstance(task.context, dict) else None
            _try_push_discuss(agent)
        else:
            role = _TYPE_TO_ROLE.get(task.task_type)
            if role:
                _try_push_next(role)
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
        # Capture context before marking done — we need the agent name for
        # discuss-task follow-up pushes.
        ctx = q().get_task_context(task_id)
        try:
            task_type = q().submit_result(task_id, result)
        except ValueError as e:
            msg = str(e)
            status_code = 404 if "not found" in msg.lower() else 400
            raise HTTPException(status_code=status_code, detail=msg)
        if task_type == "discuss":
            _try_push_discuss(ctx.get("agent") if isinstance(ctx, dict) else None)
        else:
            # Auto-transition: impl task completed → auto-enqueue review task
            if task_type == "implement" and result.status == "completed":
                _auto_enqueue_review(task_id)
            # Task done → that role is now idle → push the next pending task of the same role.
            role = _TYPE_TO_ROLE.get(task_type)
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

    return app


def _load_pane_map() -> Optional[dict]:
    path = os.getenv("AGENT_CREW_PANE_MAP")
    if not path:
        return None
    path = os.path.expanduser(path)  # Handle ~ in env var
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


app = create_app(
    db_path=os.getenv("AGENT_CREW_DB", "/tmp/agent_crew_default.db"),
    pane_map=_load_pane_map(),
    port=int(os.getenv("AGENT_CREW_PORT", "0") or 0),
)
