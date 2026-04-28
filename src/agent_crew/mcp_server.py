"""MCP server exposing the task queue to agents (Issue #106 PoC).

Agents pull work via MCP tools instead of receiving it through tmux paste-buffer
pushes. Mirrors the contract that alpha_engine's Persistent Session used and
is layered on top of the existing ``TaskQueue`` (SQLite) — no parallel state.

Tools exposed:

    get_next_task(agent, role)        → TaskRequest dict, or None
    submit_result(task_id, status,    → ack dict
                  summary, verdict,
                  findings, pr_number)
    bump_activity(task_id)            → ack dict
    get_task(task_id)                 → TaskRequest dict, or None
    list_pending(role)                → list[TaskRequest dict]

Two transports are wired up — `run_stdio()` for per-agent local launches,
`run_http(host, port)` for shared / observability use. Both share the same
in-process `TaskQueue` instance keyed on the configured DB path.

Phase 1 (this file): server + tools + parity tests.
Phase 2 (separate PR): wire into setup.py / instructions.py / cli.py so
agents actually use it.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict
from typing import Any, Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover — surfaced loud at import time
    FastMCP = None  # type: ignore

from agent_crew.loop import _resolve_verdict
from agent_crew.pipeline import (
    auto_enqueue_review,
    auto_enqueue_test,
    auto_fallback_failed_task,
)
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue

logger = logging.getLogger(__name__)

# Default role per agent. The MCP `get_next_task(agent=...)` flow falls
# back to this when no explicit ``role`` is passed, so each agent picks up
# tasks of its primary type unless an override redirects work elsewhere.
# Stays in lockstep with `setup._AGENT_TO_ROLE`; kept here so this module
# stays free of `setup` import overhead.
_DEFAULT_ROLE_FOR_AGENT: dict[str, str] = {
    "claude": "implementer",
    "codex": "reviewer",
    "gemini": "tester",
}


def _task_to_dict(task: TaskRequest) -> dict[str, Any]:
    """Serialize a TaskRequest dataclass to a JSON-friendly dict.

    Applies the same task-type guard that the tmux push path uses
    (`server._guard_description`), so MCP-pull and tmux-push deliver
    semantically identical messages — Issue #110 phase 4-b.
    """
    # Local import to avoid an import cycle: server imports from queue
    # and queue does not import server, but mcp_server is a sibling
    # module; keeping the import inside the function makes the
    # dependency direction explicit.
    from agent_crew.server import _guard_description

    payload = asdict(task)
    try:
        payload["description"] = _guard_description(task)
    except Exception:
        # If anything goes wrong with the guard logic, fall through with
        # the unprefixed description. The system prompt's precedence
        # block is the safety net.
        pass
    return payload


def build_mcp_server(
    db_path: str,
    *,
    name: str = "agent_crew",
) -> "FastMCP":
    """Construct a FastMCP server bound to the SQLite task DB at ``db_path``.

    A fresh `TaskQueue` is opened in this process — the SQLite file is the
    shared state, so multiple `build_mcp_server` calls (e.g. one per agent
    via stdio) all see the same queue.
    """
    if FastMCP is None:
        raise RuntimeError(
            "mcp package not installed — `pip install mcp` "
            "(or add it to pyproject)"
        )

    queue = TaskQueue(db_path)
    mcp = FastMCP(name)

    @mcp.tool()
    def get_next_task(
        agent: str = "",
        role: str = "",
    ) -> Optional[dict[str, Any]]:
        """Pull the next task for ``agent``.

        Resolution order matches `queue.dequeue` semantics:

        - Tasks whose ``context.agent_override`` claims this agent come
          first, regardless of task_type. Operator overrides
          (``crew run --reviewer gemini``) and the rate-limit fallback
          chain (#81) both rely on this path for dynamic role
          reassignment.
        - Otherwise the agent's *default* role is consulted — claude
          picks implement, codex picks review, gemini picks test —
          excluding tasks claimed by another agent's override. Pass an
          explicit ``role=`` to override the default.

        Returns the task as a JSON-friendly dict, or ``None`` when the
        queue has no work ready. Tasks transition to ``in_progress``
        atomically.
        """
        resolved_role = role or _DEFAULT_ROLE_FOR_AGENT.get(agent, "")
        task = queue.dequeue(agent=agent, role=resolved_role)
        if task is None:
            return None
        return _task_to_dict(task)

    @mcp.tool()
    def get_next_discuss_task(agent: str) -> Optional[dict[str, Any]]:
        """Discussion-channel variant — fan-out queue keyed on agent."""
        task = queue.dequeue_discuss_for_agent(agent)
        if task is None:
            return None
        return _task_to_dict(task)

    @mcp.tool()
    def submit_result(
        task_id: str,
        status: str = "completed",
        summary: str = "",
        verdict: Optional[str] = None,
        findings: Optional[list[str]] = None,
        pr_number: Optional[int] = None,
    ) -> dict[str, Any]:
        """Mark a task done and store its result.

        ``status`` must be one of ``completed | failed | needs_human |
        timed_out | blocked``. Other fields are optional and used by the
        review/test paths.
        """
        try:
            result = TaskResult(
                task_id=task_id,
                status=status,  # type: ignore[arg-type]
                summary=summary,
                verdict=verdict,  # type: ignore[arg-type]
                findings=list(findings or []),
                pr_number=pr_number,
            )
        except (ValueError, TypeError) as e:
            return {"acknowledged": False, "error": str(e)}
        try:
            task_type = queue.submit_result(task_id, result)
        except ValueError as e:
            return {"acknowledged": False, "error": str(e)}

        # Stage cascade — same hooks as the HTTP path so the pipeline does
        # not stall after the first stage when an agent uses MCP-only
        # delivery (#123). Push side-effects are not part of the cascade
        # contract; agents pull tasks themselves on the MCP loop.
        if task_type == "implement" and result.status == "completed":
            auto_enqueue_review(queue, task_id, pr_number=result.pr_number)
        elif task_type == "review" and _resolve_verdict(result) == "approve":
            auto_enqueue_test(queue, task_id)
        if result.status == "failed":
            auto_fallback_failed_task(queue, task_id, result, task_type)

        return {"acknowledged": True, "task_id": task_id, "task_type": task_type}

    @mcp.tool()
    def bump_activity(task_id: str) -> dict[str, Any]:
        """Heartbeat — update last_activity_at so the watchdog stays quiet
        while the agent is making slow but real progress."""
        try:
            queue.bump_activity(task_id)
        except Exception as e:
            return {"acknowledged": False, "error": str(e)}
        return {"acknowledged": True, "task_id": task_id}

    @mcp.tool()
    def get_task(task_id: str) -> Optional[dict[str, Any]]:
        """Look up a task by ID. Returns None if unknown."""
        for t in queue.list_tasks():
            if t.task_id == task_id:
                return _task_to_dict(t)
        return None

    @mcp.tool()
    def list_pending(role: str = "") -> list[dict[str, Any]]:
        """Return up to 100 pending tasks, optionally filtered by role
        (``implementer | reviewer | tester``)."""
        from agent_crew.queue import _ROLE_TO_TYPE

        tasks = queue.list_tasks(status="pending")
        if role:
            wanted = _ROLE_TO_TYPE.get(role)
            if wanted is None:
                return []
            tasks = [t for t in tasks if t.task_type == wanted]
        return [_task_to_dict(t) for t in tasks[:100]]

    @mcp.tool()
    def cancel_task(task_id: str) -> dict[str, Any]:
        """Cancel a task. Idempotent — succeeds even if the task is already
        completed or unknown (the underlying queue tolerates both)."""
        try:
            queue.cancel(task_id)
        except Exception as e:
            return {"acknowledged": False, "error": str(e)}
        return {"acknowledged": True, "task_id": task_id}

    # Stash the queue on the server so callers / tests can reach it without
    # opening a second SQLite connection.
    mcp._agent_crew_queue = queue  # type: ignore[attr-defined]
    return mcp


def run_stdio(db_path: Optional[str] = None) -> None:  # pragma: no cover
    """Entry point: ``python -m agent_crew.mcp_server`` — launches stdio MCP
    server bound to ``$AGENT_CREW_DB``."""
    db_path = db_path or os.path.expanduser(
        os.getenv("AGENT_CREW_DB", "/tmp/agent_crew_default.db")
    )
    mcp = build_mcp_server(db_path)
    logger.info("agent_crew MCP server (stdio) bound to %s", db_path)
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    run_stdio()
