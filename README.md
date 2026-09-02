# agent_crew

Multi-agent development crew system. Coordinates Claude, Codex, and Gemini agents through a FastAPI task queue, using tmux for task delivery.

```
┌─────────────────────────────────────────────────────┐
│  Coordinator (user's terminal / AI session)          │
│                                                     │
│  crew setup       →  tmux panes + worktrees         │
│  crew triage      →  AI selects GitHub issue        │
│  crew triage --watch → claims issues continuously   │
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
| `crew triage` | Auto-select and assign GitHub issues (one-shot) |
| `crew triage --watch` | **Unattended manager mode** — poll, claim, enqueue |
| `crew claims` | Inspect / release watch-mode issue claims |
| `crew status [project]` | Show queue / in-progress / completed tasks |
| `crew recover <project>` | Restart server/panes after crash |
| `crew teardown <project>` | Clean up worktrees, panes, and database |

## Unattended manager mode (recommended)

`crew triage` is one-shot: it picks an issue now and exits. That leaves a gap —
filing a GitHub issue does not by itself produce any queued work, so somebody
has to forward each issue to a management session by hand.

`crew triage --watch` closes it. It is the recommended way to run Agent Crew
unattended:

```bash
crew triage --repo owner/name --project myproject --watch --interval 5m
```

Each cycle it discovers open issues, filters to actionable ones, claims the
highest-priority candidate, and enqueues it into the normal task queue:

```
GitHub open issue → discovery → eligibility → atomic claim → enqueue
                  → implement → review → test → PR → next issue
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--watch` | off | Enable the loop. Without it, `crew triage` is unchanged. |
| `--interval` | `5m` | Poll interval. Clamped to 10s-1h. |
| `--max-cycles` | `0` | Stop after N cycles (0 = until interrupted). |
| `--max-claims` | `1` | Issues claimed per cycle. |
| `--max-attempts` | `3` | Give up on an issue after N failed claim attempts. |

**Claim safety.** A claim is a row in `issue_claims` (same SQLite file as the
task queue) taken under `BEGIN IMMEDIATE`, so exactly one watcher wins even if
several run concurrently, and the row survives a restart. The
`agent_crew:claimed` label is added on GitHub so the claim is externally
visible — but the label is *not* the lock, since `--add-label` is idempotent
and would let two watchers both "succeed".

**Eligibility.** An issue is skipped when it carries `agent_crew:done`,
`agent_crew:claimed`, or `agent_crew:hold`, when a non-terminal task already
references it, when an open PR references it, or when a declared parent
(`Parent: #N`, `depends on #N`) is still open. `agent_crew:hold` is for
"leave this alone, it's a design decision pending an operator's explicit
go-ahead" — deliberately independent of the priority policy below, since a
bare `blocked`/`p0` label there means priority 1 (claimed *first*), the
opposite of what a hold needs.

**Priority.** Deterministic: `p0`/`critical`/`security` (1) → `bug`/
`regression`/`production` (2) → unlabelled (3) → `enhancement`/`feature` (4) →
`docs`/`chore` (5). An explicit `priority:N` label overrides the policy
outright. Ties break by phase, then issue number, so the same backlog always
yields the same pick. When nothing is actionable, nothing is enqueued — the
watcher never invents work.

**Crash recovery.** `try_claim` commits before the label write and the enqueue,
so a watcher that is *killed* mid-sequence (SIGKILL, teardown, power loss)
would otherwise leave a row stuck in `claimed` — and `claimed` counts as held,
locking the issue out permanently. Every cycle therefore starts by reconciling
claims that have sat in `claimed` past `CLAIM_STALE_AFTER_SECONDS` (5 min): if
a task for that issue already exists it is adopted (no duplicate enqueue), and
if none exists the claim is released for retry and the label removed. The
staleness gate is what keeps a reconciler from stealing a live peer's in-flight
claim. If the queue can't be read, nothing is touched — recovery never guesses.

**Ledger/label divergence.** The ledger and the GitHub label are two systems
with no shared transaction, so every release writes one then the other. If the
process dies (or the label write fails) in between, the ledger says `released`
— retryable — while discovery still skips the issue for carrying the label:
retryable on paper, invisible in practice. No ordering of the two writes fixes
that, so each cycle repairs the divergence instead, clearing the label from any
issue **this** ledger has in `released`. A claim label on an issue with no local
row is left alone — it belongs to another manager, and that label is the only
cross-database signal between crews that do not share this SQLite file.
`crew claims --release` clears the label too, and says so if it cannot.

**Issue body capping.** The body fetched at discovery is persisted into the
task context, so the Context Pack has a source for the acceptance criteria
without a second GitHub call. It is capped so one enormous issue cannot bloat
every queue row — but the cap bounds *prose only*: the acceptance-criteria
section is carried across the limit verbatim, and whatever is dropped is
announced in the stored text (`[... issue body truncated by agent_crew: N
characters omitted ...]`). A capped body that still has no AC in it triggers one
bounded re-fetch of the full issue; if that fails too, the pack is marked
degraded rather than presenting itself as a complete read. "Truncated past the
acceptance criteria" must never be indistinguishable from "this issue has no
acceptance criteria".

**Failure handling.** A GitHub error backs off exponentially (30s → 15m cap);
because discovery runs before any claim, an error cycle cannot strand a claim.
If enqueue fails after a claim, the claim is released and the label removed, so
an issue is never left in a fake in-progress state — and after `--max-attempts`
releases it is marked `abandoned` rather than re-claimed forever. Inspect with
`crew claims`; hand one back with `crew claims --repo owner/name --release N`.

Workers are unaffected: they remain push-driven and never poll GitHub.

## Review round-trip

A result submission cascades into the next stage, so a normal task walks the
whole pipeline without an operator in the middle:

```
implement ✓ → review → approve      → test → merge
                     → request_changes → fix (same branch) → review → ...
```

The rejection arm is bounded. `AGENT_CREW_REVIEW_FIX_MAX_ROUNDS` (default 3)
caps how many automated fix rounds one review lineage may spend; the counter
rides the task context, so it survives a server restart and keeps counting
across the new task ids each round mints. When the budget is spent the loop
stops and says so in a PR comment rather than going quiet — a reviewer that
keeps rejecting is a disagreement another round will not settle, and the next
move is a human's. `crew run` sets `coordinator_managed`, which skips the
cascade entirely because that loop drives its own transitions.

## Build provenance

Every server reports the build it is **actually running**:

```bash
crew provenance                        # every project
crew provenance --expect 98d869d       # ...graded against a ref; exit 1 if any is stale
curl -s localhost:8105/provenance | jq
```

```
project           port commit     ref              up(s)  state
alpha_engine      8101 6583599    main            12043.2  ok
quota-core        8106 -          -                     -  STALE (pre-#248 build: no /provenance)
```

The reported SHA is captured **at process start and frozen**, together with a
content hash of the `.py` files that were loaded. That distinction is the point:
a later `git pull` moves the checkout but not the running process, so a
request-time `git rev-parse HEAD` would report the new SHA and call a stale
server current. `/provenance` instead reports both, plus
`checkout_moved_since_start` and `source_changed_since_start`.

This exists because #247 found all four live dispatchers importing an older
checkout while GitHub `main` already carried the merged context fix — so the fix
had never executed, and a before/after measurement was about to credit it with
behaviour it never produced. `build_provenance` is also written to
`context_events.jsonl` at startup, so a production cohort can be cut on the
process boundary rather than on a merge time.

`crew provenance` exits non-zero when any target cannot be confirmed current —
stale, ungradeable, **or unreachable**. A server that cannot be asked is no
evidence that it runs the expected build, so a provenance-gated validation must
not go green while one target could be on any build or absent. Pass
`--allow-unreachable` when a down project is expected; it mutes the gate for
that case only, never for a server that answered and turned out to be stale.

It is read-only: it never pulls, restarts, or repairs anything.

## Architecture

- **Push model**: server delivers tasks to agent panes via `tmux send-keys`. Agents do not poll — they receive tasks and POST results back to `POST /tasks/{id}/result`.
- **Backlog ingestion** (opt-in): `crew triage --watch` is the only component
  that polls GitHub, and it runs manager-side only.
- **Persistence**: SQLite at `~/.agent_crew/<project>/tasks.db`
- **Port**: auto-selected starting from 8100, written to `~/.agent_crew/<project>/port`
- **Worktrees**: `~/.agent_crew/<project>/{claude,codex,gemini}/`

See [docs/architecture.md](docs/architecture.md) for full design details, and
[docs/context_identity_contract.md](docs/context_identity_contract.md) for the
durable context identity + lifecycle telemetry contract external tools can
observe (task↔context attribution, retry/fallback lineage, restart recovery).

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
| `AGENT_CREW_REVIEW_FIX_MAX_ROUNDS` | `3` | Automated fix rounds per review lineage (`0` disables auto-fix) |
| `AGENT_CREW_MAIN_BRANCH` | `main` | Default main branch name |
| `GH_TOKEN` / `GITHUB_TOKEN` | — | GitHub API token (for triage/PR features) |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token (for notifications) |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID for notifications |

## License

MIT
