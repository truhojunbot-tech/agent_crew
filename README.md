# agent_crew

Multi-agent development crew system. Coordinates Claude, Codex, and Gemini agents through a FastAPI task queue, using tmux for task delivery.

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

## Installation

```bash
pip install -e .
```

Requires Python 3.10+. The `crew` CLI is installed at `~/.local/bin/crew`.

## Quick Start

```bash
# 1. Set up a project (creates worktrees + tmux panes)
crew setup myproject

# 2. Run a task (implement → review → test pipeline)
crew run "Add retry logic to the HTTP client"

# 3. Check status
crew status myproject

# 4. Tear down when done
crew teardown myproject
```

## Commands

| Command | Description |
|---------|-------------|
| `crew setup <project>` | Start server, create git worktrees, launch agent panes |
| `crew run "<task>"` | Run implementer → reviewer → tester pipeline |
| `crew discuss "<topic>"` | Send same topic to all agents for discussion |
| `crew triage` | Auto-select and assign GitHub issues |
| `crew status [project]` | Show queue / in-progress / completed tasks |
| `crew recover <project>` | Restart server/panes after crash |
| `crew teardown <project>` | Clean up worktrees, panes, and database |

## Architecture

- **Push model**: server delivers tasks to agent panes via `tmux send-keys`. Agents do not poll — they receive tasks and POST results back to `POST /tasks/{id}/result`.
- **Persistence**: SQLite at `~/.agent_crew/<project>/tasks.db`
- **Port**: auto-selected starting from 8100, written to `~/.agent_crew/<project>/port`
- **Worktrees**: `~/.agent_crew/<project>/{claude,codex,gemini}/`

See [docs/architecture.md](docs/architecture.md) for full design details.

## Security

**This tool is designed for local, single-user use only.**

- The task queue server binds to `127.0.0.1` only — it is not exposed to the network.
- There is no authentication on the HTTP API. Do not expose the server port externally.
- All secrets (GitHub token, Telegram bot token) must be set via environment variables — never hardcoded.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_CREW_DB` | `~/.agent_crew/default.db` | SQLite database path |
| `AGENT_CREW_PORT` | auto (8100+) | Server port |
| `AGENT_CREW_STATE` | auto | State file path |
| `AGENT_CREW_DELIVERY` | `tmux` | Task delivery mode (`tmux` or `mcp`) |
| `AGENT_CREW_MAIN_BRANCH` | `main` | Default main branch name |
| `GH_TOKEN` / `GITHUB_TOKEN` | — | GitHub API token (for triage/PR features) |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token (for notifications) |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID for notifications |

## License

MIT
