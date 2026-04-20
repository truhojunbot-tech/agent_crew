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
- **Do not call `GET /tasks/next` in a polling loop.** Tasks arrive via pane push only.
- **Commit and push your work before POSTing the result** when the task involves
  code changes, and include the commit hash in the summary.
- Echo `task_id` exactly as received — no prefixes, no changes.
- If the task is malformed or impossible, POST with `status: "failed"` or
  `status: "needs_human"` and a summary explaining why.
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
