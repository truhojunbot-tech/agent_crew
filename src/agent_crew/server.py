import json
import os
import subprocess
from contextlib import asynccontextmanager
from typing import Callable, Literal, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from agent_crew.protocol import GateRequest, TaskRequest, TaskResult
from agent_crew.queue import TaskQueue, _ROLE_TO_TYPE, _TYPE_TO_ROLE


class ResolveBody(BaseModel):
    status: Literal["approved", "rejected"]


def _default_push(pane_id: str, text: str) -> None:
    """Default push implementation — sends literal text then Enter to tmux pane."""
    subprocess.run(["tmux", "send-keys", "-l", "-t", pane_id, text], capture_output=True)
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

    @app.post("/tasks", status_code=201)
    def post_task(task: TaskRequest):
        task_id = q().enqueue(task)
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
        try:
            task_type = q().submit_result(task_id, result)
        except ValueError as e:
            msg = str(e)
            status_code = 404 if "not found" in msg.lower() else 400
            raise HTTPException(status_code=status_code, detail=msg)
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
