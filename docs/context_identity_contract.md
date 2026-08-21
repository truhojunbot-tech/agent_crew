# agent_crew — Durable Context Identity Contract

> status: draft
> version: 1 (`schema_version` field in every record below)
> tracks: issue #202

---

## 1. Why this exists

Agent Crew intentionally **reuses provider CLI conversations** across
tasks — `claude -p ... --continue`, `codex exec resume --last`, `agy -p
... --continue`. Reuse happens per role's worktree, for the life of the
project, because that's what lets a follow-up task pick up where the last
one left off instead of re-establishing context from scratch every time.

This document describes the metadata Agent Crew persists so an **external
observer** (for example a quota/cost tool — deliberately never named or
imported here) can answer, without any coupling to Agent Crew internals:

- which task used which context/session,
- whether that task resumed, reset, or started a context fresh,
- how many tasks that context has already served,
- whether a retry or provider fallback changed the agent/context, and
- what the task's final outcome was.

**This is telemetry/identity only.** Agent Crew is fully functional with
nothing reading these files — see `test_u_i202_works_with_no_external_consumer`
in `tests/unit/test_issue_202_context_identity.py`.

## 2. Design principle: Agent ≠ Role ≠ Context

A **context** is scoped by `(project, agent, worktree_path)` — **not**
role. Why: `agent_override` (`crew run --reviewer gemini`, or an
automatic fallback after a failure) can route a task normally owned by one
role into a *different* agent's worktree. When that happens, the CLI's
`--continue` / `resume --last` genuinely resumes whatever conversation
already lives in that worktree, regardless of which role the task belongs
to. Keying context by role would misrepresent that — two different roles
landing in the same worktree really do share one underlying provider
conversation, and the identity model says so honestly.

Concretely: `tester`'s normal task and a `reviewer` task that got
`agent_override: gemini`'d into the same gemini worktree **share one
`context_id`**. A `reviewer` task running in its own default `codex`
worktree has a **different** `context_id` from either. See
`test_u_i202_different_worktree_is_a_different_context_even_same_role`
and `test_u_i202_same_worktree_shared_across_roles`.

## 3. Where this lives

| Artifact | Format | Purpose |
|---|---|---|
| `~/.agent_crew/<project>/tasks.db` → `context_state` table | SQLite | current context identity per `(project, agent, worktree_path)`; durable, survives restart |
| `~/.agent_crew/<project>/tasks.db` → `task_attribution` table | SQLite | one row per task, with context/lineage/outcome fields |
| `~/.agent_crew/<project>/attribution.jsonl` | append-only JSONL | **at least two** lines per task — one at dispatch (`status="in_progress"`), one more when it reaches a terminal state (`status`/`outcome`/`completed_at` populated) — always a verbatim `task_attribution` row (never hand-built separately, so it can't drift from the DB shape). Consumers take the most recent line per `task_id` as current state. For consumers that outlive the DB or want to tail rather than query. |
| `~/.agent_crew/<project>/context_events.jsonl` | append-only JSONL | lifecycle event stream (§6) — kept **separate** from `attribution.jsonl` on purpose, since events have a different, heterogeneous shape per `event_type` and mixing them would be a breaking change for existing `attribution.jsonl` readers |

## 4. `context_state` (durable identity, one row per context)

| Column | Meaning |
|---|---|
| `context_key` | `f"{project}::{agent}::{worktree_path}"` — primary key |
| `context_id` | stable UUID; the thing external tools should key spend/usage by |
| `context_generation` | increments each time the context is reset (starts at 1) |
| `session_task_index` | how many tasks this context (current generation) has served so far |
| `provider_session_id` | provider-native session/thread id, when observed (currently: best-effort for claude only, via its `stream-json` output; null for codex/agy — neither reliably exposes one on stdout today) |
| `last_task_id` | the most recent task_id dispatched into this context |

A context is created fresh (`context_generation = 1`) the first time a
task ever dispatches into a given `(project, agent, worktree_path)`. It is
**reset** (new `context_id`, `context_generation += 1`) only when a task
explicitly requests it via `task.context["context_reset"] = true` — this
is the deterministic, caller-controlled trigger required by the issue's
acceptance criteria. Ordinary worktree file resets (`git checkout .` /
`git clean -fd`, run after every task) do **not** reset the context —
they clean tracked/untracked files, not the provider's own conversation
state for that directory.

**Dispatch is serialized per resolved context, not just per role.** Role
slots (`implementer`/`reviewer`/`tester`) alone don't prevent two
*different* roles from resolving into the *same* `(agent, worktree_path)`
— e.g. `agent_override` routing a `reviewer` task into `gemini`'s worktree
while `tester`'s own gemini task is also pending. Running both
`--continue` processes against the same directory at once would corrupt
that one provider conversation, and would also make `session_task_index`/
`previous_task_id` meaningless (two tasks racing to claim "next"). The
dispatcher tracks an `active_worktrees` set alongside the existing
`active_roles` set: a task whose resolved worktree is already in flight
under a different role is put back on the queue and retried on the next
poll tick rather than dispatched concurrently. See
`test_u_i202_concurrent_dispatch_into_shared_worktree_is_serialized`.

## 5. `task_attribution` (one row per task)

Existing fields (`task_id, project, agent, role, task_type,
worktree_path, codex_logs_path, repo_url, git_branch, created_at,
updated_at, status`) are unchanged. Added in schema_version 1:

| Column | Meaning |
|---|---|
| `schema_version` | contract version this row was written under |
| `model` | model string, when reliably known (currently: gemini only, via its explicit `--model` flag; empty for claude/codex, which rely on their own CLI/config defaults with no equivalent flag here) |
| `context_id` | see §4 |
| `provider_session_id` | see §4, copied at dispatch time |
| `context_policy` | `"fresh"` or `"resume"` for this task (`"compact"` and `"unknown"` are valid values reserved for future use — see §6's `context_compacted` note) |
| `context_generation` | see §4, snapshotted at dispatch time |
| `session_task_index` | this task's position within the context (1 = first task in a fresh context) |
| `previous_task_id` | the task_id that last used this context slot before this one — set across a reset too, so lineage can be stitched across the generation boundary |
| `retry_of` | task_id this task is retrying, when it was created by the auto-retry path (`task.context["retry_attempt"]` present) |
| `fallback_of` | task_id this task is a provider fallback for, when created by the auto-fallback path (`task.context["fallback_from_task_id"]`) |
| `started_at` | dispatch time (unix seconds) |
| `completed_at` | terminal-result time (unix seconds); set once, regardless of whether the result arrived via the agent's own `POST /tasks/{id}/result` or an internal dispatcher failure path |
| `outcome` | the task's final status; for failures, `f"failed:{reason}"` when a structured reason is available (e.g. `failed:agy_quota_exhausted`), else just the raw status |

## 6. `context_events.jsonl` — lifecycle events

Every line is `{"schema_version": 1, "event_type": "...", "ts": "<ISO8601 UTC>", ...}`
plus event-specific correlation fields (always includes whichever of
`task_id, project, role, agent, context_id` are relevant/known at that
point). Event types:

- `context_created` — first-ever dispatch into a `(project, agent, worktree_path)`; `context_generation == 1`.
- `context_resumed` — a later dispatch into an existing context, within the same Agent Crew process that's been tracking it since it last created/recovered it.
- `context_recovered` — like `context_resumed`, but this is the **first** time *this process* has touched a context row it didn't create — i.e. the row was written by an earlier process. Directly evidences "context/task lifecycle survives Agent Crew restart" rather than leaving it to be inferred.
- `context_reset` — a `context_reset`-flagged dispatch that bumped the generation (`context_generation > 1`).
- `context_compacted` — **best-effort, observational only.** None of claude/codex/agy currently emit a structured "conversation compacted" signal Agent Crew can rely on; this fires only when a plain-text marker (e.g. "conversation compacted") happens to appear in that task's own dispatch log output. A miss does not mean a compaction didn't happen.
- `provider_fallback` — this task's actual `agent` differs from its role's *configured default* agent (`from_agent`, `to_agent`). Fires for both explicit `--reviewer gemini`-style overrides and automatic post-failure fallback routing.
- `task_started` — emitted right before the provider subprocess is spawned.
- `task_completed` — a task reached `status="completed"` via `POST /tasks/{id}/result`.
- `task_failed` — a task reached a failed/terminal-non-success state, from either `POST /tasks/{id}/result` (agent self-reported) or an internal dispatcher failure path (timeout, quota, exit code, no worktree, etc. — `_fail_if_active`).

## 7. Privacy / safety

None of the tables or event streams above ever contain credentials, full
prompts, source code, or conversation contents. Fields are limited to
identifiers (task/context/project/role/agent/model), timestamps, booleans,
and short enum-like strings (`context_policy`, `outcome`, event type,
transient-error tag names). This is intentional: the contract is meant to
be safe for a public runtime integration to consume.

## 8. Versioning

`schema_version` (currently `1`) is bumped only for **breaking** changes
to field names/meanings/removal. Purely additive fields (new optional
columns, new event types) do not require a bump — treat unknown fields
and unknown `event_type` values as forward-compatible and ignorable.

## 9. Non-goals (out of scope for this contract)

- No token counting or pricing logic lives in Agent Crew.
- No dependency on any external quota/analytics package.
- No adaptive context optimizer — Agent Crew exposes the data; deciding
  what to do with it (compact earlier, switch providers, cap spend) is an
  external tool's job.
