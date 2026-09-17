# Runtime pause / STOP (#311)

**Status: implemented and unit-tested. No organic canary evidence yet** — the
issue requires bounded organic evidence attached to the parent incident before a
wider resume, and that has not been produced here. See
[What is not proven](#what-is-not-proven).

## The gap this closes

A feeder-level STOP upstream prevented new dispatches being *handed to* this
runtime, and the runtime kept draining the tasks already in its queue. STOP
looked effective from above while work continued below it.

The cause is structural: nothing in the execution path asked *"am I allowed to
start anything right now"*. The only guard lived in the thing that supplies work.

## Where the gate lives

Inside `TaskQueue.dequeue` and `TaskQueue.dequeue_discuss_for_agent` — the two
places every transport claims work:

| caller | path |
|---|---|
| HTTP `GET /tasks/next` | `server.py` |
| headless dispatcher loop | `server.py` |
| `_try_push_next` (tmux push) | `server.py` |
| MCP `get_next_task` | `mcp_server.py` |

Putting the guard *inside* the claim rather than beside it is the point: a guard
at the call sites is one new caller away from being bypassed, which is how the
incident happened. A prior review in this repo made the same argument about
transports — "a guard on one transport is a guard an agent walks around by
changing how it reports" (#123, and again in #305's review).

## Scopes

| scope | where it lives | survives restart |
|---|---|---|
| `project` | `runtime_pause` row in the project's own `tasks.db` | yes, exactly as the queue does |
| `global` | JSON file at `$AGENT_CREW_GLOBAL_PAUSE_FILE` | yes |

**Any paused scope blocks.** A project cannot run its way out of a runtime-wide
STOP — that would make the global scope advisory, which is the shape of the bug.
When both are paused the *global* one is reported first, so an operator clearing
a local STOP is told there is a wider one behind it.

⛔ The global path has **no baked-in default**. Absent env means this runtime has
no global scope, and a standalone install keeps working. A hardcoded shared
location would be a deployment assumption a packaged runtime cannot make.

⛔ A global pause file that exists but cannot be read means **paused**. "A global
STOP exists and I cannot read it" is exactly when guessing is unsafe; "no global
scope configured" is not. The asymmetry is deliberate.

## Generations, not timestamps

Every activation increments a monotonic per-scope generation, including one that
arrives while already paused — a second incident during a STOP is a *new* reason
to be stopped.

Resume must name the generation it believes it is clearing, and is **refused** if
a newer STOP has landed since. A resume naming no generation is refused too: it
cannot prove it knows what it is clearing.

Time cannot decide this. Two clocks and a retry are enough to make "later"
meaningless, and the failure mode is resuming into an uncleared incident.

## Operator surface

```
crew pause <project> --reason "incident" --incident ALFRED-39
crew pause-status <project>
crew resume <project> --generation <n>
```

```
GET  /pause            → decision + per-scope state
POST /pause            → {scope, reason, source, incident_ref}
POST /resume           → {scope, generation}; 409 on a stale generation
```

A stale resume answers **409**, not 200-with-a-flag: a caller that only reads the
status code must not read a refused resume as success.

## Product boundary

`PORTABLE_CORE`. This runtime owns a pause **state** and a pause **decision**.
Deciding *when* a fleet should stop belongs to whatever drives it, and nothing
here imports a fleet — asserted by a test that parses imports across the whole
package. An external system manager drives the CLI/API above and reads the status
back; the dependency direction is one-way.

## Fail-closed and serialization (review of PR #312)

Three properties the first version did not have:

- **The claim gate runs inside the claim's own `BEGIN IMMEDIATE`**, which
  serializes against `activate_pause`. Checking before the transaction left a
  window where a STOP could be persisted after the check and before the
  `pending -> in_progress` commit, so a pre-STOP item could start *after* the
  safety boundary.
- **Every unreadable pause state blocks.** Project row, global file, and the
  cascade gate all fail closed. "I could not determine whether we are paused" is
  not evidence that we are not.
- **Resume is a compare-and-swap.** The generation comparison and the clear
  happen in one transaction (and under an exclusive lock for the global file),
  so a resume that had already read an older generation cannot overwrite a STOP
  written in between — a comparison cannot see a write it never read.

## Blocked-transition receipts

A `dequeue` returning `None` is indistinguishable from an empty queue, so every
refusal is now recorded durably in `blocked_transitions` with the transition,
scope, reason, source, incident, generation, and the task/context/provider/
session identities. `list_blocked_transitions()` reads them; an empty queue
leaves no receipt, which is what makes a receipt mean something.

## What is not proven

1. **No organic canary.** Every test here is synthetic. The acceptance criterion
   asking for bounded organic evidence on the parent incident is not met by this
   change alone.
2. **In-flight cancellation is bounded, not immediate.** A task already running
   in an agent completes its current atomic action; what STOP prevents is the
   *next* claim. Cooperative mid-flight cancellation is not implemented, and the
   issue explicitly allows that as the default safety mode — but it means a STOP
   does not stop an agent mid-turn.
3. ~~The cascade is not yet gated.~~ **Now gated** (review of PR #312).
   `_try_push_next`, fallback, retry, `auto_enqueue_review`/`test`/`fix` and
   `auto_merge_pr` each consult the shared decision before starting, so a result
   arriving for a task that was already in flight when STOP landed creates no
   child, retry or next-stage work. The result itself is still recorded — only
   new work is refused.
