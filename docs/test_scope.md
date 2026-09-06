# Test scope — what the tester runs (#272)

The tester role used to run the full test suite for every project,
unconditionally. CI already runs the full suite on the PR, so that was a second
copy of the same work on a shared developer host. alpha_engine#5541 measured
the cost: **load 30.29 on 16 cores, 1 GB free, a dev job queued 45+ minutes**,
with two `make test` runs starting 69 s apart in the same worktree.

Two things changed.

## 1. Scope is configuration, and the default is targeted

The tester is now told to run the tests the diff needs plus cross-cutting
guards, and **not** the full suite. This is not conditional on any project
name — a per-project config decides, and a project that configures nothing gets
the targeted default. Full-suite is opt-**in**.

Resolution order, most authoritative first:

| # | source | when to use it |
|---|--------|----------------|
| 1 | `AGENT_CREW_TEST_SCOPE` — inline JSON, or a path to a JSON file | one-off, or a host-wide emergency clamp |
| 2 | `~/.agent_crew/<project>/test_scope.json` | operator override; **outranks the repo on purpose**, so turning the scope down under load never needs a commit to the repo whose tests are the problem |
| 3 | `<worktree>/.agent_crew/test_scope.json` | the project's own answer, versioned with the code it describes |
| 4 | built-in default | targeted, no full suite |

### Schema

```json
{
  "full_suite": false,
  "targeted": ["python3 -m pytest {paths} -q"],
  "guards":   ["ruff check .", "python3 -m pytest tests/contract -q"],
  "full":     ["make test"]
}
```

- **`targeted`** — how to run tests for the files the diff touched. `{paths}`
  is substituted by the tester from `git diff --name-only origin/main...HEAD`.
  Omit it and the tester derives the targeting itself.
- **`guards`** — cross-cutting checks that run whatever the diff touched.
  Contract tests, lint, typecheck: the things targeted tests structurally
  cannot catch. Omit and the tester runs the project's lint once.
- **`full`** — used only when `full_suite` is true.
- **`full_suite`** — set `true` for a project whose CI does *not* cover the
  suite, or where the tester's run is the only one.

A malformed file is logged and ignored rather than fatal — a tester that cannot
start is worse than one running slightly the wrong commands — but the warning
names the file, so an override that is not in effect does not look like one
that is.

### The tester must say what it ran

The prompt requires the `summary` to name the scope and the commands. Without
that, a targeted pass and a full pass produce the same result shape and nobody
can tell whether any of this took effect. The tester may still decide a diff
needs the full suite and run it — but not silently.

## 2. One test stage per worktree, enforced outside the process

The dispatcher already refused two concurrent tasks per worktree, but that
guard is a `set` in one process's memory. It does not survive a dispatcher
restart while a `start_new_session=True` child keeps running, and it cannot see
a second dispatcher at all — which is how two `make test` runs began 69 s apart.

Test-stage dispatches now take an `flock` before launching. The lock is keyed
on the worktree's real path but kept **outside** it, under
`~/.agent_crew/locks/`, so every process on the host resolves the same file
regardless of which project's state directory it started from, and no lock file
lands in the repo under test.

- Acquisition is **non-blocking**: a task that cannot get the lock is requeued
  and retried on the next tick, the same shape as the existing
  worktree-collision branch. Parking a role slot on a blocking acquire would
  trade a concurrency bug for a stall.
- The lock covers the **test stage only**. Gating implement/review on it would
  serialise the whole crew behind a slow test run.
- If the lock cannot be created at all (read-only filesystem, say), the
  dispatch proceeds unlocked and logs a warning. A hygiene mechanism must not
  become an outage.
