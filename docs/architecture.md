# agent_crew — Architecture

> status: draft
> version: 0.2

---

## 1. System Overview

```
┌─────────────────────────────────────────────────────┐
│  Coordinator (user's terminal / AI session)          │
│                                                     │
│  crew setup       →  tmux panes + worktrees         │
│  crew triage      →  AI selects GitHub issue        │
│  crew discuss     →  enqueue discussion tasks       │
│  crew run         →  enqueue implement/review tasks  │
│  crew status      →  session + task status          │
└──────────────┬──────────────────────────────────────┘
               │  HTTP :<auto-port>
       ┌───────▼────────┐
       │  Task Queue +  │  FastAPI + SQLite
       │  Gate Server   │  (background process)
       └───────┬────────┘
               │
    ┌──────────┼──────────┐
    ▼          ▼          ▼
  pane .1    pane .2    pane .3
  claude     codex      gemini
 worktree   worktree   worktree
```

---

## 2. Components

### 2.1 Task Queue + Gate Server

- **Runtime**: FastAPI + uvicorn, runs as a background process
- **Port**: auto-selected starting from 8100; written to `/tmp/agent_crew/<project>/port`
- **Persistence**: SQLite at `/tmp/agent_crew/<project>/agent_crew.db`
- **Atomicity**: `GET /tasks/next` wraps dequeue in a DB transaction (no double-assignment)
- **Crash recovery**: SQLite persists all state; on restart, pending/in_progress tasks are preserved. `crew recover` or coordinator auto-restart relaunches the server. Agents retry on connection refused (3 attempts, exponential backoff).

#### HTTP API — Tasks

| Method | Path | Description |
|--------|------|-------------|
| POST | `/tasks` | Enqueue a new task |
| GET | `/tasks/next` | Atomic dequeue (`?agent=<name>&role=<role>`) |
| POST | `/tasks/{id}/result` | Submit task result |
| GET | `/tasks/{id}` | Get task status |
| GET | `/tasks` | List tasks (`?status=pending\|in_progress\|completed`) |
| DELETE | `/tasks/{id}` | Cancel a task |

#### HTTP API — Gates

| Method | Path | Description |
|--------|------|-------------|
| POST | `/gates` | Create a human approval gate |
| GET | `/gates/pending` | List pending gates (coordinator polls this) |
| POST | `/gates/{id}/resolve` | Resolve gate (`{"status": "approved"\|"rejected"}`) |
| GET | `/gates/{id}` | Get gate status (crew polls this) |

#### Task schema (POST /tasks)

```json
{
  "task_id": "t-001",
  "task_type": "implement | review | test | discuss",
  "description": "natural language prompt",
  "branch": "agent/feat-xyz",
  "priority": 3,
  "context": {}
}
```

#### Result schema (POST /tasks/{id}/result)

```json
{
  "task_id": "t-001",
  "status": "completed | failed | needs_human",
  "summary": "...",
  "verdict": "approve | request_changes | null",
  "findings": ["..."],
  "pr_number": 42
}
```

#### Gate schema (POST /gates)

```json
{
  "id": "gate-001",
  "type": "approval | merge | escalation",
  "message": "Issue #42: Fix login timeout — proceed?",
  "status": "pending",
  "created_at": 1713500000
}
```

---

### 2.2 Session Manager

Tracks tmux pane lifecycle per agent.

**State file**: `/tmp/agent_crew/<project>/sessions.json`

```json
{
  "claude":  {"pane": "myproject:0.1", "started_at": 1713500000, "failures": 0, "cmd": "claude --dangerously-skip-permissions --continue"},
  "codex":   {"pane": "myproject:0.2", "started_at": 1713500000, "failures": 0, "cmd": "codex --dangerously-bypass-approvals-and-sandbox"},
  "gemini":  {"pane": "myproject:0.3", "started_at": 1713500000, "failures": 0, "cmd": "gemini --yolo"}
}
```

**Health check**:
```bash
tmux capture-pane -t <pane_target> -p | tail -3
# no output or no prompt → pane dead → restart agent
```

**Refresh conditions**:
- `started_at` age > `SESSION_MAX_HOURS` (default: 24h)
- `failures` >= `SESSION_MAX_FAILURES` (default: 2)

**Refresh action** (agent-agnostic — uses `cmd` from sessions.json):
```bash
tmux send-keys -t <pane> "" Enter
tmux send-keys -t <pane> "<agent cmd from sessions.json>" Enter
# update sessions.json: started_at = now, failures = 0
```

**Default resume commands** (stored in sessions.json `cmd` field at setup time):

| Agent | Command |
|-------|---------|
| Claude | `claude --dangerously-skip-permissions --continue` |
| Codex | `codex exec resume --last --full-auto --skip-git-repo-check` |
| Gemini | `gemini --resume latest --sandbox false --yolo` |

---

### 2.3 Task Discovery (Triage + Poll)

```
crew triage
  ↓
Fetch open GitHub issues (gh issue list --json)
  ↓
Assign triage agent (default: gemini) to analyze + select
  ↓
Agent returns: selected issue + task description
  ↓
crew creates approval gate (POST /gates)
  ↓
crew polls GET /gates/{id} until resolved
  ↓
if approved: crew run "<task description>" auto-invoked
if rejected: skip, offer next candidate (or exit)
```

```
crew poll --interval 1h
  ↓
Loop: run triage → gate → run (if approved) → sleep interval → repeat
  ↓
Timeout on gate → auto-reject, continue to next cycle
```

---

### 2.4 Discussion Loop

```
crew discuss "<topic>"
  ↓
Coordinator writes topic to Task Queue (task_type=discuss, role=panel, for each agent)
  ↓
Each agent: GET /tasks/next?role=panel → gets topic + assigned perspective
  ↓
Each agent: performs analysis from perspective → POST /tasks/{id}/result
  ↓
Coordinator: collects all results → synthesizes → writes synthesis.md
  ↓
if --rounds N > 1: next round uses synthesis as new context
if --then-run: synthesis becomes input to code-review loop
```

**synthesis.md format**:
```markdown
## Topic
<original topic>

## Panel Opinions
### <agent> (<perspective>)
<opinion>
...

## Synthesis
<coordinator conclusion>

## Decision
<actionable next step>
```

---

### 2.5 Code-Review Loop

TDD is the default. The coder writes failing tests first, then implements. The reviewer evaluates test quality and business logic — not just test passage.

```
crew run "<task>"
  ↓
Coordinator: POST /tasks (task_type=implement, assigned to coder)
  ↓
Coder: GET /tasks/next
  → write failing tests (RED)
  → implement until tests pass (GREEN)
  → refactor
  → commit → create PR
  → POST /tasks/{id}/result
  ↓
Coordinator: POST /tasks (task_type=review, assigned to reviewer)
  ↓
Reviewer: GET /tasks/next → 3-layer review:
  1. Test quality  — do tests verify requirements? edge cases? error paths?
  2. Code quality  — architecture, readability, coupling
  3. Business gaps — side effects, security, perf, missing requirements
  → POST /tasks/{id}/result {verdict}
  ↓
if approved:
  [optional] Coordinator: POST /tasks (task_type=test, assigned to tester)
  Tester: GET /tasks/next → run full suite in clean env → POST /tasks/{id}/result
  if passed: done ✅
if request_changes (and iter < max_iter):
  Coordinator: re-enqueue implement task with review feedback → loop
if max_iter reached: create escalation gate → surface to user ⚠️
```

---

### 2.6 Multi-Agent Setup

`crew setup <project>` execution order:

1. Assert current directory is a git repo
2. For each agent: `git worktree add $AGENT_CREW_WORKTREES/<project>/<agent>/ agent/<agent>`
3. Generate agent-specific instruction file in each worktree:
   - `CLAUDE.md` in the claude worktree
   - `AGENTS.md` in the codex worktree
   - `GEMINI.md` in the gemini worktree
   - Content: common section (Task Queue API protocol, workflow steps, result format) + agent-specific section (role instructions). See requirements.md §15 for template.
4. Split tmux panes in current window (coordinator stays in pane 0)
5. Launch agent CLI in each pane from its worktree
6. Start Task Queue + Gate server in background, write port file
7. Write `sessions.json` (including per-agent restart command)

---

## 3. Package Structure

```
agent_crew/
├── pyproject.toml
├── README.md
├── src/
│   └── agent_crew/
│       ├── __init__.py
│       ├── protocol.py     # TaskRequest, TaskResult, GateRequest (dataclasses)
│       ├── queue.py         # TaskQueue + GateQueue core (thread-safe, SQLite-backed)
│       ├── server.py        # FastAPI Task Queue + Gate HTTP server
│       ├── session.py       # SessionManager (tmux pane lifecycle)
│       ├── triage.py        # GitHub issue triage + poll loop
│       ├── discussion.py    # Discussion loop orchestration
│       ├── loop.py          # Code-review loop orchestration
│       ├── instructions.py  # Agent instruction file generator (CLAUDE.md/AGENTS.md/GEMINI.md)
│       ├── setup.py         # Multi-agent environment setup
│       └── cli.py           # `crew` CLI entry point (click)
└── tests/
    ├── test_queue.py
    ├── test_session.py
    ├── test_triage.py
    ├── test_discussion.py
    └── test_loop.py
```

---

## 4. Data Flow: Files and Directories

```
$HOME/.agent_crew/worktrees/<project>/<agent>/   git worktrees (persistent)
/tmp/agent_crew/<project>/
  port              Task Queue server port
  sessions.json     session state (including agent restart commands)
  agent_crew.db     SQLite task + gate store
  synthesis.md      discussion output (overwritten each run)
```

Nothing written inside the user's git repo. All runtime state in `/tmp`.

---

## 5. Runtime Dependencies

| Dependency | Purpose |
|------------|---------|
| `fastapi` | Task Queue + Gate HTTP server |
| `uvicorn` | ASGI server for FastAPI |
| `httpx` | Coordinator → Task Queue HTTP client |
| `click` | CLI framework |

Python >= 3.11. No other runtime dependencies.

Agent CLIs (claude, codex, gemini) must be installed separately by the user.
