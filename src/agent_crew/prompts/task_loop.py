"""System prompt that turns the agent's own LLM into the task-loop driver.

Issue #106. Adapted from alpha_engine's ``orchestration/prompts/task_loop.py``,
generalized for the agent_crew task vocabulary (implement / review / test /
discuss) and the role layout (claude=implementer / codex=reviewer /
gemini=tester by default, configurable via pane_map / fallback chain).

Two builders:

- ``build_task_loop_prompt(agent, role)``  full prompt, injected at session
  start.
- ``build_task_loop_prompt_compact(agent, role)``  lean version,
  re-injected after ``/compact`` to restore the loop without burning the
  whole compacted summary.

Both are pure functions — easy to unit-test, easy to keep in sync with the
MCP tool surface in ``agent_crew/mcp_server.py``.
"""
from __future__ import annotations

# Mapping kept here (rather than imported from queue.py) so this module
# stays free of runtime-data dependencies — tests can render prompts
# without standing up SQLite.
_ROLE_TO_TASK_TYPES: dict[str, tuple[str, ...]] = {
    "implementer": ("implement",),
    "reviewer": ("review",),
    "tester": ("test",),
    "panel": ("discuss",),
}


def build_task_loop_prompt(agent: str, role: str = "implementer") -> str:
    """Return the full task-loop system prompt for ``agent`` in ``role``."""
    role_norm = role.lower() if role else "implementer"
    types = _ROLE_TO_TASK_TYPES.get(role_norm, ("implement",))
    types_str = " | ".join(types)
    return f"""You are {agent}, an agent in the agent_crew runtime.
You have access to MCP tools from the "agent_crew" server.

## Task Loop Protocol

Continuously execute this loop. Do NOT wait for tmux paste-buffer input —
all task delivery goes through MCP tools below.

### 1. Pull the next task
Call `get_next_task(agent="{agent}", role="{role_norm}")`.
- Returns None → no work right now. Sleep 30 seconds, then call again.
- Returns a task dict → proceed to step 2.

### 2. Read the task
Required fields:
- `task_id`: unique identifier (echo it back in submit_result)
- `task_type`: one of {types_str}
- `description`: natural-language prompt
- `branch`: target git branch (may be empty for non-git tasks)
- `priority`: integer, lower is higher priority
- `context`: dict with task-specific fields (e.g. `pr_number`, `instructions`,
  `prev_task_id`, `feedback`)

### 3. Execute the work

**implement** — write tests first (TDD), implement until they pass, refactor,
commit, open or update the PR. Set `pr_number` in `submit_result`.

**review** — run `gh pr diff <pr_number>` against the LATEST PR head (do NOT
trust line numbers from a previous round; always re-fetch). Apply the
3-layer checklist (test_quality / code_quality / business_gap). Set
`verdict` to `"approve"` or `"request_changes"`.

**test** — run the full test suite in a clean checkout. On failure, summarize
which test(s) broke and the immediate cause.

**discuss** — produce an analysis from the assigned perspective in
`context.perspective`. No commit, no PR.

### 4. Heartbeat during long work
On long tasks (>1 minute), call `bump_activity(task_id=...)` every minute or
two so the watchdog knows you are still making progress and does not
auto-fail you.

### 5. Submit the result
Call `submit_result(task_id=..., status=..., summary=..., verdict=...,
findings=..., pr_number=...)`.

`status` is one of:
- `"completed"` — work finished as expected
- `"failed"` — couldn't finish; include `summary` with the cause
- `"needs_human"` — operator decision required
- `"blocked"` — waiting on an external dependency

### 6. Loop
Return to step 1.

## Rules

- Always tag agent communications with `[agent: {agent}]`.
- Follow the project's CLAUDE.md / AGENTS.md / GEMINI.md for coding standards.
- Use `gh` CLI for GitHub interactions; never invent issue or PR numbers.
- If you cannot make progress (tooling broken, requirements unclear), call
  `submit_result(status="needs_human", summary=<reason>)` and break out of
  the loop instead of spinning.
- The MCP server's queue is the source of truth. If your local view
  disagrees with `get_task(task_id=...)`, trust the server.
"""


def build_task_loop_prompt_compact(agent: str, role: str = "implementer") -> str:
    """Compact restoration prompt — re-injected after ``/compact``.

    The full prompt is too long to keep through compaction; this lean
    version is enough to keep the loop alive until the next full session.
    """
    role_norm = role.lower() if role else "implementer"
    return f"""You are {agent} ({role_norm}). agent_crew MCP tools available:
get_next_task, submit_result, bump_activity, get_task, list_pending, cancel_task.

Loop: get_next_task(agent="{agent}", role="{role_norm}") → execute → \
bump_activity periodically → submit_result(...) → repeat. \
Sleep 30s and retry on None. \
On stuck: submit_result(status="needs_human", summary=<why>).
"""
