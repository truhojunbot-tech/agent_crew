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
Start this background loop immediately when the session starts. Keep it running in the background for the entire session:
```bash
while true; do
  RESP=$(curl -sf 'http://localhost:<port>/tasks/next?role=<role>')
  if [ -n "$RESP" ] && [ "$RESP" != "null" ]; then
    TASK_ID=$(echo "$RESP" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('task_id',''))")
    DESC=$(echo "$RESP" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('description',''))")
    echo "=== TASK_ASSIGNED task_id=$TASK_ID ==="
    echo "description: $DESC"
    echo "POST when done: curl -X POST http://localhost:<port>/tasks/$TASK_ID/result -H 'Content-Type: application/json' -d '{\"task_id\":\"'$TASK_ID'\",\"status\":\"completed\",\"summary\":\"<summary>\"}'"
  fi
  sleep 30
done &
```
When you see TASK_ASSIGNED output, immediately process the task and run the POST curl shown.

## Result Submission (MANDATORY)
After completing any task, you MUST call the API POST request below before doing anything else:
```bash
curl -X POST http://localhost:<port>/tasks/{id}/result \\
  -H "Content-Type: application/json" \\
  -d '{
    "task_id": "<id>",
    "status": "completed",
    "summary": "<short summary of what was done>"
  }'
```
Required fields: `task_id`, `status`, `summary`.
Status values (accepted by the API): `completed` | `failed` | `needs_human`.
Review tasks may additionally include `verdict` (`approve` | `request_changes`) and `findings` (list of strings).

### Result Note Template
Use this structure for the handoff note you write before the API POST:
```text
status: completed | failed | needs_human
summary: <short description of outcome>
branch: <branch-name>
commit: <commit-hash>
notes: <context or follow-up details>
```
`branch`, `commit`, and `notes` are for your own log — the API accepts them via `summary` text.
Never skip the POST request just because the note is complete.

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
  if [ -n "$RESP" ] && [ "$RESP" != "null" ]; then
    TASK_ID=$(echo "$RESP" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('task_id',''))")
    DESC=$(echo "$RESP" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('description',''))")
    echo "=== TASK_ASSIGNED task_id=$TASK_ID ==="
    echo "description: $DESC"
    echo "POST when done: curl -X POST http://localhost:<port>/tasks/$TASK_ID/result -H 'Content-Type: application/json' -d '{\"task_id\":\"'$TASK_ID'\",\"status\":\"completed\",\"summary\":\"<summary>\"}'"
  fi
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
  if [ -n "$RESP" ] && [ "$RESP" != "null" ]; then
    TASK_ID=$(echo "$RESP" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('task_id',''))")
    DESC=$(echo "$RESP" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('description',''))")
    echo "=== TASK_ASSIGNED task_id=$TASK_ID ==="
    echo "description: $DESC"
    echo "POST when done: curl -X POST http://localhost:<port>/tasks/$TASK_ID/result -H 'Content-Type: application/json' -d '{\"task_id\":\"'$TASK_ID'\",\"status\":\"completed\",\"summary\":\"<summary>\"}'"
  fi
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
  if [ -n "$RESP" ] && [ "$RESP" != "null" ]; then
    TASK_ID=$(echo "$RESP" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('task_id',''))")
    DESC=$(echo "$RESP" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('description',''))")
    echo "=== TASK_ASSIGNED task_id=$TASK_ID ==="
    echo "description: $DESC"
    echo "POST when done: curl -X POST http://localhost:<port>/tasks/$TASK_ID/result -H 'Content-Type: application/json' -d '{\"task_id\":\"'$TASK_ID'\",\"status\":\"completed\",\"summary\":\"<summary>\"}'"
  fi
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
