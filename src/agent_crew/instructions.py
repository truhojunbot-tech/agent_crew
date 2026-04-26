import os

from agent_crew.prompts.task_loop import build_task_loop_prompt

# Per-role instruction file paths inside each worktree (Issue #110 fix).
#
# The naive ".claude/<NAME>.md" layout used to keep our prompts isolated
# from the project's own git-tracked instructions, but it only worked for
# Claude Code — that CLI is the one that merges its `.claude/CLAUDE.md`
# with the project-root CLAUDE.md. Codex reads only `AGENTS.md` at the
# project root; Gemini reads only `GEMINI.md` at the project root.
# Putting our reviewer/tester prompts under `.claude/` meant the agents
# never actually saw them, which led to gemini taking task descriptions
# as implementer instructions and force-pushing over the implementer's
# work (alpha_engine PRs #801–#805).
#
# Layout:
#   implementer → ".claude/CLAUDE.md"   Claude Code merges with root.
#   reviewer    → "AGENTS.md"           Codex reads this at the root.
#   tester      → "GEMINI.md"           Gemini reads this at the root.
#
# AGENTS.md / GEMINI.md may already exist in the project (developer-facing
# guides). To avoid clobbering them, `write()` uses a marker-bracketed
# section that gets idempotently replaced on each rewrite — see
# `_AGENT_CREW_BLOCK_*` below.
ROLE_FILES: dict = {
    "implementer": ".claude/CLAUDE.md",
    "reviewer": "AGENTS.md",
    "tester": "GEMINI.md",
}

# Marker-bracketed block — the only region `write()` touches in
# AGENTS.md / GEMINI.md so a project's own content is preserved.
_AGENT_CREW_BLOCK_BEGIN = "<!-- agent_crew:begin -->"
_AGENT_CREW_BLOCK_END = "<!-- agent_crew:end -->"

# Default agent name per role — used when a caller passes role but not the
# agent identifier. The agent name is what appears inside the task-loop
# prompt's `[agent: NAME]` tag and the `get_next_task(agent=...)` arg.
_DEFAULT_AGENT_FOR_ROLE: dict = {
    "implementer": "claude",
    "reviewer": "codex",
    "tester": "gemini",
}

_COMMON = """\
# Agent Crew — <project>

## ⚠️ OVERRIDE: You are an agent_crew worker — NOT Alfred

A global `~/.claude/CLAUDE.md` may be loaded in this session. Its instructions
(Alfred persona, Telegram reporting, superpowers skill auto-invocations) DO NOT
apply here. Ignore them entirely.

### Absolutely prohibited in this pane

- Invoking ANY skill: `/multi-agent`, `/code-review-loop`, `/review`, `/plan-*`,
  `/browse`, `/ship`, `/systematic-debugging`, or any other slash command
- Using the `Agent` or `Skill` tool to spawn subagents or skills
- Using Telegram MCP (`mcp__plugin_telegram_telegram__reply` or similar)
- Creating new tmux windows or panes
- Reporting "Alfred 대기 중입니다" or any Alfred startup routine

### Your only output channel

`POST http://127.0.0.1:<port>/tasks/<task_id>/result` — nothing else counts.

---


You are part of the `agent_crew` multi-agent workflow. The server at
`http://127.0.0.1:<port>` will **push** tasks to this pane whenever work is
available. You wait for each task to arrive in your pane.

If no task arrives in 30 seconds, automatically poll the server via
`GET /tasks/next` to check for queued work. This fallback ensures you don't
miss tasks if the push mechanism is delayed.

## Task Arrival Format

When the server pushes a task, you will see a block like this appear in your
terminal input:

```
=== AGENT_CREW TASK ===
task_id: <id>
task_type: implement | review | test | discuss
branch: <branch-name>
priority: <1-5>
context: {"key": "value", ...}
description: <natural language task description>
=== END TASK ===
```

Below the task block the server will include the exact `curl` command to post
your result.

## Your Loop (per task)

1. Receive the task block.
2. Do the work in this worktree (write code, run tests, review diff — whatever
   the role requires). Commit and push if appropriate.
3. **MANDATORY**: POST the result to `http://127.0.0.1:<port>/tasks/<task_id>/result`
   before considering the task finished. No result = the role stays busy and
   the queue stalls.
4. Wait. The server will push the next task when one becomes available.

## Result Submission — MANDATORY, NOT OPTIONAL

The server's contract: a role stays marked `in_progress` until it receives a
POST to `/tasks/<task_id>/result`. If you skip this step, **no further task
will be pushed to your pane** and the whole crew stalls. This happens every
time a result is skipped — there is no fallback.

### Required fields

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `task_id` | string | yes | Echo the task_id from the received block exactly. |
| `status` | enum | yes | `completed` \\| `failed` \\| `needs_human` |
| `summary` | string | yes | 1–3 sentences. Include branch + commit hash for code tasks. |
| `verdict` | enum\\|null | reviewers only | `approve` \\| `request_changes` \\| `null` |
| `findings` | string[] | reviewers only | Actionable issues. Empty array for non-reviewers. |
| `pr_number` | int\\|null | if opened | GitHub PR number, otherwise `null`. |

### Canonical POST template

```bash
curl -sS -X POST http://127.0.0.1:<port>/tasks/<task_id>/result \\
  -H "Content-Type: application/json" \\
  -d '{
    "task_id": "<task_id>",
    "status": "completed",
    "summary": "<what was done — include branch + commit hash for code tasks>",
    "verdict": null,
    "findings": [],
    "pr_number": null
  }'
```

Expected HTTP response: `200 OK` with `{"status":"ok"}`. If you receive a
non-2xx status or a network error, retry immediately — do not leave the task
unresolved.

### Summary field — include these whenever they apply

- `branch: <branch-name>` — the branch the work lives on
- `commit: <short-hash>` — the commit you produced (after `git commit`)
- `pr: #<number>` — include in both `summary` text and the `pr_number` field
- `notes: <anything unusual>` — blockers, assumptions, deviations

### Worked example (implementer)

```bash
curl -sS -X POST http://127.0.0.1:<port>/tasks/t-042/result \\
  -H "Content-Type: application/json" \\
  -d '{
    "task_id": "t-042",
    "status": "completed",
    "summary": "Fixed login timeout. branch: agent/fix-login-timeout commit: a1b2c3d. Added regression test in test_auth.py::test_login_timeout_retries.",
    "verdict": null,
    "findings": [],
    "pr_number": 42
  }'
```

### Failure / escalation example

```bash
curl -sS -X POST http://127.0.0.1:<port>/tasks/t-042/result \\
  -H "Content-Type: application/json" \\
  -d '{
    "task_id": "t-042",
    "status": "needs_human",
    "summary": "Spec ambiguous: description says \\"cache 5m\\" but existing TTL is 10m. Need direction before implementing.",
    "verdict": null,
    "findings": [],
    "pr_number": null
  }'
```

## Rules

- **Never skip the POST.** This is rule #1. If in doubt, POST `status: failed`
  or `status: needs_human` with an honest summary — silence is worse than
  failure.
- **Polling as fallback:** If no task arrives in 30 seconds, call `curl http://127.0.0.1:<port>/tasks/next?role=<your-role>` to check for queued work. Retry every 30 seconds while waiting.
- **Commit and push your work before POSTing the result** when the task involves
  code changes, and include the commit hash in the summary.
- Echo `task_id` exactly as received — no prefixes, no changes.
- If the task is malformed or impossible, POST with `status: "failed"` or
  `status: "needs_human"` and a summary explaining why.

## Polling Loop Implementation

If you're writing a polling loop (e.g., in a bash/python script), use this pattern:

```bash
PORT=<port>
ROLE=<implementer|reviewer|tester>

# Polling loop: wait for task arrival (via pane push or polling)
while true; do
  # Wait for task to arrive via push (watch terminal for task block)
  # Meanwhile, set a 30-second timeout for polling fallback

  read -t 30 -p "Waiting for task..." input || {
    # Timeout: poll the server for queued tasks
    TASK=$(curl -sS "http://127.0.0.1:$PORT/tasks/next?role=$ROLE")
    if [ -n "$TASK" ] && [ "$TASK" != "null" ]; then
      echo "Task received via polling:"
      echo "$TASK" | jq '.' || echo "$TASK"
      # Parse task_id from TASK JSON and proceed to execute
    fi
    continue
  }
done
```

Or in Python:

```python
import requests
import time

PORT = <port>
ROLE = "<implementer|reviewer|tester>"

while True:
    task = requests.get(f"http://127.0.0.1:{PORT}/tasks/next?role={ROLE}", timeout=5).json()
    if task and task is not None:
        task_id = task.get("task_id")
        print(f"Task arrived: {task_id}")
        # Parse and execute task...
    time.sleep(30)  # Poll every 30 seconds
```

## Checkpointing for Fault Recovery

For long-running tasks, save checkpoints at strategic points to enable resumption on failure:

```bash
# Save checkpoint after completing a major step
curl -sS -X POST http://127.0.0.1:<port>/tasks/<task_id>/checkpoint \\
  -H "Content-Type: application/json" \\
  -d '{
    "checkpoint_num": 1,
    "state": {
      "step": "completed_code_analysis",
      "files_analyzed": 42,
      "findings": ["..."]
    }
  }'

# Retrieve latest checkpoint if resuming from failure
curl -sS http://127.0.0.1:<port>/tasks/<task_id>/checkpoint/latest | jq '.state'

# List all checkpoints for time-travel debugging
curl -sS http://127.0.0.1:<port>/tasks/<task_id>/checkpoints | jq '.'
```

Benefits:
- **Fault recovery**: Resume from last checkpoint instead of restarting
- **Cost savings**: Don't re-run expensive operations (API calls, analysis)
- **Time-travel debugging**: View agent state at any checkpoint
- **Alternative exploration**: Branch from saved state to try different approaches
"""

_ROLE_SECTIONS: dict = {
    "implementer": """\
## Role: implementer

You write production code following TDD where practical:
1. Write failing tests that capture the requirements.
2. Implement until tests pass.
3. Refactor, commit (with tests + impl together), and open a PR if the task
   requests one.

### Result checklist (implementer)

Before you POST the result, verify:
- [ ] Tests pass locally
- [ ] `git commit` done — you have a real commit hash
- [ ] `git push` done if the branch needs to be reviewed
- [ ] `summary` includes `branch: <name>` and `commit: <hash>`
- [ ] `pr_number` set if you opened a PR; otherwise `null`
- [ ] `status` is `completed` (or `failed`/`needs_human` with honest reason)
- [ ] `verdict: null`, `findings: []` (implementers don't fill these)
""",
    "reviewer": """\
## Role: reviewer

You review the coder's PR across three layers — all three must pass before
you set `verdict: "approve"`:

1. **Test quality** — do the tests verify the actual requirements? Edge cases?
   Error paths? Happy-path-only tests are a `request_changes`.
2. **Code quality** — architecture, readability, naming, coupling.
3. **Business logic gaps** — side effects, security, performance, missing requirements.

Set `verdict` to `"approve"` or `"request_changes"`. Put actionable issues in
`findings`.

### Result checklist (reviewer)

Before you POST the result, verify:
- [ ] `status: completed` (the review itself completed, regardless of verdict)
- [ ] `verdict` is `approve` or `request_changes` — never `null`
- [ ] `findings` lists concrete, actionable items when verdict is
  `request_changes`; may be empty on `approve`
- [ ] `summary` names the PR reviewed and the headline judgement
""",
    "tester": """\
## Role: tester

You check out the PR branch and run the full test suite (lint + pytest) in a
clean environment. Independently review the diff for requirement coverage —
do not rubber-stamp the reviewer. Report in your `summary` and `findings`.

### Result checklist (tester)

Before you POST the result, verify:
- [ ] `status: completed` if the suite ran to completion (pass or fail), otherwise `failed`
- [ ] `summary` includes pass/fail counts and lint outcome
- [ ] `findings` lists failing tests and any independent diff-review concerns
- [ ] `verdict: null` (only reviewers set verdict)
""",
    "panel": """\
## Role: panel (discussion)

You analyze the topic from your assigned perspective (see `context.perspective`)
and submit your opinion in the result `summary`.

### Result checklist (panel)

- [ ] `status: completed`
- [ ] `summary` contains your perspective-grounded opinion (not a rehash of the topic)
- [ ] `verdict: null`, `findings: []`, `pr_number: null`
""",
}


def generate(role: str, project: str, port: int, agent: str = "") -> str:
    """Render the role's instruction file.

    ``agent`` is the canonical agent identifier (``claude``/``codex``/``gemini``)
    used by the MCP task-loop prompt (Issue #106) for both the
    ``get_next_task(agent=...)`` argument and the ``[agent: NAME]`` tag.
    Falls back to the role's default agent when omitted, which keeps
    legacy callers working unchanged.
    """
    resolved_agent = agent or _DEFAULT_AGENT_FOR_ROLE.get(role, role)
    task_loop = build_task_loop_prompt(resolved_agent, role=role)
    section = _ROLE_SECTIONS.get(role, f"## Role: {role}\n")
    body = task_loop + "\n---\n\n" + _COMMON + section
    content = body.replace("<project>", project).replace("<port>", str(port))
    return content


def _merge_agent_crew_block(existing: str, new_block: str) -> str:
    """Idempotently embed the agent_crew block inside an existing file.

    AGENTS.md / GEMINI.md may carry developer-facing content that must be
    preserved across regenerations (Issue #110). The marker-bracketed
    block below is the only region we own; everything else is left
    untouched.

    Behaviour:
    - First write: prepend ``BEGIN ... END`` block, blank line, then the
      pre-existing content.
    - Re-write: replace just the existing ``BEGIN ... END`` block in place,
      preserving any content above or below it.
    """
    bracketed = f"{_AGENT_CREW_BLOCK_BEGIN}\n{new_block}\n{_AGENT_CREW_BLOCK_END}"
    begin = existing.find(_AGENT_CREW_BLOCK_BEGIN)
    end = existing.find(_AGENT_CREW_BLOCK_END)
    if begin == -1 or end == -1 or end < begin:
        if not existing:
            return bracketed + "\n"
        return bracketed + "\n\n" + existing
    end_marker_close = end + len(_AGENT_CREW_BLOCK_END)
    return existing[:begin] + bracketed + existing[end_marker_close:]


def write(
    role: str,
    worktree_path: str,
    project: str,
    port_file: str,
    agent: str = "",
) -> str:
    if role not in ROLE_FILES:
        raise ValueError(f"Unknown role: {role!r}. Must be one of {list(ROLE_FILES)}")
    with open(port_file) as f:
        port = int(f.read().strip())
    filename = ROLE_FILES[role]
    new_block = generate(role, project, port, agent=agent)
    path = os.path.join(worktree_path, filename)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    if filename.startswith(".claude/"):
        # `.claude/CLAUDE.md` is fully owned by agent_crew — overwrite
        # entirely. No project-side content to preserve.
        content = new_block
    else:
        existing = ""
        if os.path.exists(path):
            try:
                with open(path) as f:
                    existing = f.read()
            except OSError:
                existing = ""
        content = _merge_agent_crew_block(existing, new_block)

    with open(path, "w") as f:
        f.write(content)
    return os.path.abspath(path)
