import os

ROLE_FILES: dict = {
    "implementer": "CLAUDE.md",
    "reviewer": "AGENTS.md",
    "tester": "GEMINI.md",
}

_COMMON = """\
# Agent Crew — <project>

## Task Queue (HTTP)
- Receive next task: GET http://localhost:<port>/tasks/next?role=<role>
- Submit result:     POST http://localhost:<port>/tasks/{id}/result

## Polling Routine (MANDATORY)
Start this background loop immediately when you receive this instruction:
```bash
while true; do
  RESP=$(curl -sf 'http://localhost:<port>/tasks/next?role=<role>')
  if [ -n "$RESP" ]; then
    echo "NEW_TASK received"
    # Process the task here
  fi
  sleep 30
done &
```

## Result Submission (MANDATORY)
After completing any task, you MUST call:
```bash
curl -X POST http://localhost:<port>/tasks/{id}/result \\
  -H "Content-Type: application/json" \\
  -d '{
    "status": "done",
    "branch": "<branch>",
    "commit": "<commit_hash>",
    "notes": "<any issues or observations>"
  }'
```
Status values: done | blocked | needs_clarification

## Common Instructions
- Follow TDD: write tests first, then implement
- Commit and push when done
- NEVER skip result submission — coordinator depends on it
"""

_ROLE_SECTIONS: dict = {
    "implementer": """\
## Role: implementer
- Write production code in src/
- Run pytest and fix failures before committing
- Branch: agent/claude

## Implementer Polling
```bash
while true; do
  RESP=$(curl -sf 'http://localhost:<port>/tasks/next?role=implementer')
  if [ -n "$RESP" ]; then echo "NEW_TASK: $RESP"; fi
  sleep 30
done &
```
""",
    "reviewer": """\
## Role: reviewer
- Review diffs and open GitHub PRs
- Leave structured feedback in result.md
- Do not merge without approval gate

## Reviewer Polling
```bash
while true; do
  RESP=$(curl -sf 'http://localhost:<port>/tasks/next?role=reviewer')
  if [ -n "$RESP" ]; then echo "NEW_TASK: $RESP"; fi
  sleep 30
done &
```
""",
    "tester": """\
## Role: tester
- Write and maintain tests in tests/
- Ensure full coverage of new code paths
- Report flaky tests immediately

## Tester Polling
```bash
while true; do
  RESP=$(curl -sf 'http://localhost:<port>/tasks/next?role=tester')
  if [ -n "$RESP" ]; then echo "NEW_TASK: $RESP"; fi
  sleep 30
done &
```
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
