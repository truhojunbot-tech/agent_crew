# G12: per-task execution state (D6)

SEV-0 gate G12 (alfred#51 c5777790815 §2; RECONCILIATION.md F9, D6).
Branch `sev0/g12-exec-state-instrumentation`. Not deployed.

## discovery

The preserved DBs had 17 `tasks` columns, and `push_at` was 0 on 6801 of 6806
rows. Only the tmux push path ever wrote `push_at`, and almost every task
runs through the dispatcher. Nothing recorded who claimed a task, where it was
sent, whether it was still alive, or which build handed it out.

## result

The migration is additive (`ALTER TABLE … DEFAULT NULL`). Rows written before
it read NULL, meaning "not recorded", never 0 or ''. It adds:
- **16 snapshot columns** on `tasks`;
- **`task_exec_events`**, an append-only history of `claimed`, `dispatched`,
  `pushed`, `requeued`, `result` and `force_failed`, ordered by `event_id`.

`GET /tasks/{id}` adds an `execution` object: the snapshot plus `events`.
Every field it returned before is unchanged.

| Field | Written by | Meaning |
|---|---|---|
| `claimed_at`, `claimed_by_role`, `claimed_by_agent`, `claimed_via` | `dequeue` / `dequeue_discuss_for_agent`, inside the claiming transaction | `claimed_via` is `tmux_push`, `dispatcher`, `mcp` or `http_poll`. `claimed_by_agent` is NULL when the claimer names no agent; the provider then appears in `dispatch_agent`. |
| `claim_build_commit`, `claim_code_fingerprint` | same | `provenance.build()` of the claiming process: the running build `/health` reports, not disk HEAD (F5). |
| `dispatched_at`, `dispatch_channel`, `dispatch_agent`, `dispatch_target`, `dispatch_attempt` | `record_dispatch`, after the hand-off | Channel is `tmux_pane`, `claude_p`, `codex_exec`, `gemini_cli` or `api`. Target is a pane id, `pid:<n>`, `mcp:<agent>` or `http_poll:<agent>`. The attempt count carries across requeues. |
| `lease_owner`, `lease_expires_at` | `record_dispatch`; cleared by requeue, result and force-fail | For the dispatcher, the expiry is the kill timeout it enforces. It is NULL for a pane, because a pane task is reaped on idleness (#231), not on a deadline. |
| `last_heartbeat_at`, `last_heartbeat_source` | `record_heartbeat` (snapshot only, no event) | Source is `process_alive` (dispatcher, every `AGENT_CREW_HEARTBEAT_INTERVAL` s, default 30), `pane_busy` (watchdog) or `worker_checkpoint`. These are observations, not the agent asserting progress. |
| `push_at` | unchanged (#152) | Still written only on the tmux path, because the watchdog's idle clock reads it; writing it elsewhere would change a timeout decision. `dispatched_at` covers every channel. |
| `result_posted_at` | `submit_result` | Agent POSTs and internal failures (`_fail_if_active`) alike. A `force_fail` ends the lease but leaves this NULL, because no result was posted. |

**Recording only.** Nothing in dispatch reads these fields. Every recorder
catches its own failure, and the claim and end records sit under a SAVEPOINT
that rolls back alone. Tests show that dispatch picks the same tasks with the
recorders disabled, and that a missing history table blocks no claim, requeue
or result.

## Not covered

- `dispatch_ack_at` from D6 is not implemented. No worker acknowledges
  receipt, and a server-side guess would be a fabricated value.
- Nothing is backfilled. The historical rows stay NULL, because that is what
  was recorded about them.
