import os

ROLE_FILES: dict = {
    "implementer": "CLAUDE.md",
    "reviewer": "AGENTS.md",
    "tester": "GEMINI.md",
}

_COMMON = """\
# Agent Crew — <project>

You are part of the `agent_crew` multi-agent workflow. The server at
`http://127.0.0.1:<port>` will **push** tasks to this pane whenever work is
available. You do NOT poll — you wait for each task to arrive in your pane.

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
3. POST the result to `http://127.0.0.1:<port>/tasks/<task_id>/result` with:

```bash
curl -s -X POST http://127.0.0.1:<port>/tasks/<task_id>/result \\
  -H "Content-Type: application/json" \\
  -d '{
    "task_id": "<task_id>",
    "status": "completed",
    "summary": "<short description of what you did>",
    "verdict": null,
    "findings": [],
    "pr_number": null
  }'
```

Status values: `completed | failed | needs_human`.
Reviewers may set `verdict` to `approve | request_changes` and add `findings`.
Include `pr_number` if you opened one.

4. Wait. The server will push the next task when one becomes available.

## Rules

- **Never skip the POST.** The coordinator tracks task state via these results.
  Without a POST, the role stays marked busy and the next task will not be pushed.
- **Do not call `GET /tasks/next` in a polling loop.** Tasks arrive via pane push only.
- **Commit and push your work before POSTing the result** when the task involves code changes.
- If the task is malformed or impossible, POST with `status: "failed"` or `status: "needs_human"`
  and a summary explaining why.
"""

_ROLE_SECTIONS: dict = {
    "implementer": """\
## Role: implementer

You write production code following TDD where practical:
1. Write failing tests that capture the requirements.
2. Implement until tests pass.
3. Refactor, commit (with tests + impl together), and open a PR if the task
   requests one.

Include in your result `summary`: what was done, commit hash, and branch name.
Set `pr_number` if you opened a PR.
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
""",
    "tester": """\
## Role: tester

You check out the PR branch and run the full test suite (lint + pytest) in a
clean environment. Independently review the diff for requirement coverage —
do not rubber-stamp the reviewer. Report in your `summary` and `findings`.
""",
    "panel": """\
## Role: panel (discussion)

You analyze the topic from your assigned perspective (see `context.perspective`)
and submit your opinion in the result `summary`.
""",
}


def generate(role: str, project: str, port: int) -> str:
    section = _ROLE_SECTIONS.get(role, f"## Role: {role}\n")
    content = (_COMMON + section).replace("<project>", project).replace("<port>", str(port))
    return content


def write(role: str, worktree_path: str, project: str, port_file: str) -> str:
    if role not in ROLE_FILES:
        raise ValueError(f"Unknown role: {role!r}. Must be one of {list(ROLE_FILES)}")
    with open(port_file) as f:
        port = int(f.read().strip())
    filename = ROLE_FILES[role]
    content = generate(role, project, port)
    path = os.path.join(worktree_path, filename)
    with open(path, "w") as f:
        f.write(content)
    return os.path.abspath(path)
