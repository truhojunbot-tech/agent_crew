# agent_crew — Requirements

> status: draft
> version: 0.2
> see also: architecture.md

---

## 1. Overview

`agent_crew` is a standalone runtime that combines four patterns into a single package:

1. **Multi-agent setup** — tmux pane + git worktree environment per AI agent
2. **Discussion loop** — panel debate → coordinator synthesis → decision/plan
3. **Code-review loop** — implement → review → test → approve cycle (auto-retry)
4. **Task discovery** — AI triage of GitHub issues + polling loop with human approval gate

Target users: developers who run multiple AI coding agents (Claude, Codex, Gemini, or any CLI-based agent) collaboratively on a single codebase.

---

## 2. Goals

- **Zero local path coupling**: all configuration via env vars or CLI flags; no hardcoded paths
- **No vendor lock-in**: agent-agnostic; any CLI that can read stdin / write stdout qualifies
- **No Docker required**: tmux pane as the execution unit
- **Persistent across restarts**: task state survives tmux session kill/restart
- **Self-contained**: installable via `pip install agent_crew`; no project-specific dependencies

---

## 3. Non-Goals

- No GUI / web dashboard (CLI only)
- No cloud execution (local machine only, at least v1)
- No agent implementation — `agent_crew` orchestrates agents, does not provide them
- No CI/CD integration (out of scope for v1)
- No external notification (Telegram, Slack, etc.) — handled by the coordinator agent, not agent_crew
- No webhook integration — external triggers call `POST /tasks` directly

---

## 4. Concepts

### 4.1 Project

A named unit of work. Maps to a git repository and a set of agent panes.

### 4.2 Agent

A CLI process running in a tmux pane. Must be able to pick up a task, perform it, and report the result. Supported out of the box: `claude`, `codex`, `gemini` (extendable).

### 4.3 Task

A unit of work assigned to one agent.

- `task_type`: `implement | review | test | discuss`
- `status`: `pending → assigned → in_progress → completed | failed | cancelled`
- `priority`: 1 (highest) – 5 (lowest)

### 4.4 Session

A tmux pane running an agent process. Auto-restarted when idle too long or after repeated failures.

---

## 5. Loop Types

Two first-class workflows. They can be chained: a discussion produces a synthesis that becomes the input to a code-review loop.

```
crew discuss "<topic>"             →  discussion loop  →  synthesis.md
crew run "<task>"                  →  code-review loop →  approved PR
crew discuss "<topic>" --then-run  →  both, chained automatically
```

### 5.1 Discussion Loop

All agents participate as **equal panel members**. No implementer/reviewer split. The Coordinator synthesizes opinions into a decision or plan.

**Use cases**:
- Strategy planning before coding
- Architecture review (pass a design doc as topic; `--then-run` chains to implementation)
- Retrospective / post-mortem analysis
- Autonomous improvement loop (agents iteratively refine strategy/code across rounds)

Each agent is assigned a **perspective**: `analyst | critic | advocate | risk` (customizable).

Supports multiple rounds: each round uses the previous synthesis as new context.

#### Example: Architecture → Implementation Task Breakdown

The discussion loop is the recommended way to decompose an architecture document into concrete implementation tasks before coding begins.

```bash
crew discuss "Break this architecture into vertical slice implementation tasks. \
  Each slice must be independently testable and deliver working functionality." \
  --context docs/architecture.md \
  --perspectives "claude:analyst,codex:critic,gemini:risk" \
  --then-run
```

Flow:
```
Each agent reads architecture.md from their assigned perspective
  → analyst: identifies natural dependency boundaries and slice order
  → critic:  challenges slice definitions — too large? missing dependencies?
  → risk:    flags slices where design is unclear or assumptions are wrong
  ↓
Coordinator synthesizes into ordered slice list with completion criteria
  ↓
synthesis.md contains:
  - Slice N: "<what it delivers>" — completion criterion: "<how to verify it works>"
  ↓
--then-run: first slice becomes the crew run task automatically
```

This replaces ad-hoc planning conversations: the panel surfaces disagreements before a single line of code is written, and the synthesis is a concrete, verifiable task list rather than vague notes.

### 5.2 Code-Review Loop

TDD is the default philosophy: the coder writes failing tests first, then implements until all tests pass. The reviewer's job is not just to verify test passage but to evaluate whether the tests themselves are meaningful.

**Implementation step (TDD)**:
1. Write failing tests that capture the requirements
2. Implement until tests pass
3. Refactor, commit, open PR

**Review step (3-layer)**:
1. **Test coverage** — do the tests actually verify the requirements, not just pass? Are edge cases, error paths, and boundary conditions covered?
2. **Code quality** — architecture, readability, naming, coupling
3. **Business logic** — what the tests don't capture: side effects, security, performance, missing requirements

A PR where tests pass but the tests only cover the happy path is a `request_changes`. The reviewer must explicitly check all three layers.

**Tester step**: independent runner executes the full test suite in isolation to catch environment-specific failures.

Repeats until approved or max iterations reached. Default role assignment: `claude` (implement) → `codex` (review) → `gemini` (test). All configurable.

---

## 6. Task Discovery

### 6.1 crew triage

AI analyzes GitHub issues and selects the next task to work on.

Flow:
```
Fetch open GitHub issues (unprocessed)
  → AI agent reads issues + recent merge history
  → selects the highest-priority issue
  → converts to task description
  → creates approval gate via POST /gates
  → on approval: crew run is invoked automatically
  → on rejection: issue is skipped, next candidate offered
```

Output: structured task handed off to code-review loop.

### 6.2 crew poll

Runs `crew triage` repeatedly on a schedule. Intended for autonomous operation with a human approval gate.

```
crew poll --interval 1h
```

Each cycle:
1. Run triage → candidate issue selected
2. Create approval gate → wait for coordinator to resolve
3. If approved: run code-review loop
4. If no response within timeout: skip and wait for next cycle

---

## 7. Human Gates

Gates never block inside the terminal. The user must never have to enter the tmux session to respond.

**Mechanism: HTTP gate endpoints** (on the Task Queue server)

When a gate is required, `crew` creates a gate via HTTP and polls for resolution:

```
POST /gates                    crew creates pending gate
GET  /gates/pending            coordinator polls for pending gates
POST /gates/{id}/resolve       coordinator resolves (approved/rejected)
GET  /gates/{id}               crew polls until resolved
```

**Coordinator's responsibility**

The coordinator agent monitors `GET /gates/pending`, surfaces the request to the user via its own communication channel, receives the response, and calls `POST /gates/{id}/resolve`. No stdin or file access required.

**Gate types**:
- `approval` — proceed with task? (triage, poll)
- `merge` — merge PR?
- `escalation` — loop stuck, abort or retry?

**Timeout**: if no response within `--timeout` duration, gate auto-rejects and crew skips to next action.

---

## 8. Multi-Agent Setup

`crew setup <project>`:
- Creates git worktree per agent
- Splits tmux panes in current window (no new session)
- Generates agent-specific instruction file in each worktree:
  - `CLAUDE.md` in the claude worktree
  - `AGENTS.md` in the codex worktree
  - `GEMINI.md` in the gemini worktree
  - Each file contains: (1) common HTTP API protocol, (2) agent-specific role and task type
- Starts Task Queue server in background
- Tracks session state

---

## 9. CLI

```bash
crew setup <project> [--agents claude,codex,gemini]   # setup environment
crew triage [options]                                  # AI picks next GitHub issue
crew poll [--interval 1h]                             # run triage on a schedule
crew discuss "<topic>" [options]                       # run discussion loop
crew run "<task>" [options]                            # run code-review loop
crew status [<project>]                               # show session + task status
crew recover <project>                                # restore after tmux restart
crew logs [--task <id>]                               # show task history
crew teardown <project>                               # cleanup worktrees + server
```

`crew triage` options:
```
--repo <owner/repo>     GitHub repository (default: current git remote)
--agent <name>          agent to run triage (default: gemini)
--no-confirm            skip approval gate, run immediately
```

`crew poll` options:
```
--interval <duration>   polling interval (default: 1h, min: 10m)
--repo <owner/repo>     GitHub repository
--timeout <duration>    approval gate timeout before skipping (default: 30m)
```

`crew discuss` options:
```
--agents <list>           panel agents (default: all setup agents)
--rounds <n>              discussion rounds (default: 1)
--perspectives <map>      agent:perspective (e.g. claude:analyst,codex:critic)
--then-run                chain into code-review loop using synthesis as task
--output <path>           save synthesis (default: comm_dir/synthesis.md)
```

`crew run` options:
```
--coder <agent>       agent for implementation (default: claude)
--reviewer <agent>    agent for review (default: codex)
--tester <agent>      agent for testing (default: gemini)
--no-tester           skip test step
--max-iter <n>        max review iterations (default: 5)
--branch <name>       branch name (auto-generated if omitted)
--pr <number>         attach to existing PR
```

---

## 10. Recovery

`crew recover <project>` restores the full environment after a tmux restart:
- Recreates panes and restarts agents with `--continue`
- Restarts Task Queue server (pending tasks loaded from SQLite)
- Resumes any in-progress loop

---

## 11. Security

- No credentials stored in any project file
- All runtime state (`sessions.json`, `agent_crew.db`, `port`) lives in `/tmp` — not in repo
- All secrets passed via environment variables
- Public repo must contain no user-specific paths, tokens, or hostnames

---

## 12. Environment Variables

```
AGENT_CREW_WORKTREES   base path for git worktrees (default: $HOME/.agent_crew/worktrees)
AGENT_CREW_COMM_DIR    base path for comm dirs     (default: /tmp/agent_crew)
AGENT_CREW_PORT        force a fixed server port   (default: auto, starting from 8100)
SESSION_MAX_HOURS      session age before refresh  (default: 24)
SESSION_MAX_FAILURES   consecutive failures before refresh (default: 2)
```

---

## 13. Agent Resume Commands

Each agent CLI has a different flag for resuming a previous session:

| Agent | Resume command |
|-------|---------------|
| Claude | `claude --dangerously-skip-permissions --continue` |
| Codex | `codex exec resume --last --full-auto --skip-git-repo-check` |
| Gemini | `gemini --resume latest --sandbox false --yolo` |

`crew setup` stores the full command in `sessions.json` so that `SessionManager` and `crew recover` are agent-agnostic.

---

## 14. Task Queue Server Recovery

The Task Queue server is a background process managed by the coordinator.

**Crash detection**: `crew status` and `crew recover` check if the port file exists and if the process is alive.

**Recovery policy**:
1. `crew recover <project>` restarts the server automatically (pending tasks survive in SQLite)
2. If the coordinator agent detects the server is down (HTTP connection refused), it restarts the server before retrying
3. Agents that fail to reach the server fall back to retry with exponential backoff (max 3 retries, then mark task as `needs_human`)

---

## 15. Instruction File Templates

`crew setup` generates an instruction file in each agent's worktree. The file name matches what each agent CLI reads automatically:

| Agent | File | Default role |
|-------|------|-------------|
| Claude | `CLAUDE.md` | implement |
| Codex | `AGENTS.md` | review |
| Gemini | `GEMINI.md` | test / triage |

**Common section** (included in all files):

```markdown
# Agent Crew Protocol

## Task Queue API
- Server: http://localhost:<port> (read port from /tmp/agent_crew/<project>/port)
- Poll for tasks: GET /tasks/next?agent=<your_name>&role=<your_role>
- Submit result: POST /tasks/{id}/result

## Workflow
1. Read the port file to discover the server address
2. Poll GET /tasks/next with your agent name and role
3. If no task available, wait 10s and retry
4. Execute the task in your worktree
5. Submit result via POST /tasks/{id}/result
6. Return to step 2

## Result format
POST /tasks/{id}/result with JSON body:
- status: "completed" | "failed" | "needs_human"
- summary: brief description of what was done
- verdict: "approve" | "request_changes" (review tasks only)
- findings: list of issues found (review tasks only)
- pr_number: PR number if created
```

**Agent-specific section** (appended per role):

- **Implementer**:
  > You are the coder. Follow TDD: write failing tests first that capture the requirements, then implement until all tests pass, then refactor. Create a branch, commit with tests and implementation together, and open a PR.

- **Reviewer**:
  > You are the reviewer. Your job is not to check if tests pass — that is the tester's job. You review three things in order:
  > 1. **Test quality**: do the tests actually verify the requirements? Check edge cases, error paths, and boundary conditions. If the tests only cover the happy path, that is a request_changes.
  > 2. **Code quality**: architecture, readability, naming, unnecessary coupling.
  > 3. **Business logic gaps**: what the tests don't capture — side effects, security, performance, missing requirements.
  > Submit verdict: "approve" only if all three layers pass.

- **Tester**:
  > You are the tester. Check out the PR branch and run the full test suite in a clean environment. Report pass/fail and any environment-specific failures not caught in the coder's local run.

- **Panel** (discussion):
  > You are a panel member. Analyze the topic from your assigned perspective and submit your opinion.

---

## 16. Implementation TODOs

These are not design questions — they are resolved during implementation:

- **Instruction file prompt tuning**: start with the §15 template, iterate based on actual agent behavior
- **File polling fallback**: implement only if an agent CLI proves unable to call HTTP (all known agents can)
