# Crew runtime swap and recovery

Run `scripts/runtime_swap.sh PROJECT FULL_SHA preflight`, then `scripts/runtime_swap.sh PROJECT FULL_SHA go --decision-id T0-ID --actor NAME`, then `scripts/runtime_swap.sh PROJECT FULL_SHA post`. Each project must have `~/.agent_crew/PROJECT/fleet.env` and `~/.agent_crew/PROJECT/cea.env`. `fleet.env` uses one literal `KEY=VALUE` per line, permits comments beginning with `#`, and may be empty; it holds fleet keys such as `AGENT_CREW_CLOUD_*`. Keep `HOME`, `USER`, `LOGNAME`, `PATH`, `LANG`, `LC_*`, `PYTHONPATH`, and `AGENT_CREW_CEA_*` out of it. The CEA file holds only `AGENT_CREW_CEA_*` keys. A CEA override pointing outside that project's `cea.env` is refused. The new process receives the swap invoker's required `HOME` and `PATH`, optional `USER`, `LOGNAME`, `LANG`, and `LC_*` when present, then `fleet.env`, `cea.env`, and `PYTHONPATH` set to the selected checkout's `src`. The captured live environment is never a launch source.

Every step holds `~/.agent_crew/PROJECT/swap.lock` through completion and records the holder in `swap.pid`. A concurrent launcher refuses and names the holder PID. `go` repeats `AGENT_CREW_SWAP_PRECHECK` and checks `--decision-id` against the current signed, unexpired T0 snapshot for the project and target SHA immediately before stopping the old listener. Supply the owner principal suffix explicitly with `--actor NAME` or `AGENT_CREW_SWAP_ACTOR` (for example, `hojun` for `owner:hojun`). A missing actor or invalid decision leaves the old listener running. After a verified `post`, the script checks `/health` for every crew with a `~/.agent_crew/*/state.json`. It atomically points `~/.agent_crew/cli_src` at the selected checkout only when every crew serves the target build; otherwise it reports each unverified path or port and leaves the link unchanged. A `cli_src` problem does not change a verified `POST_OK` result.

Preflight writes `env_diff.json` with **key names only** under `~/.sev0-evidence/crew-swap-PROJECT-SHA7/`. It refuses if live and declared key sets differ. To acknowledge a reviewed difference, pass `--accept-env-diff KEY1,KEY2` with exactly the union of names in `live_but_undeclared` and `declared_but_not_live`; use the same option for `go`. Values are never printed in the diff or error message. The private `env.pre.nul` is retained only for recovery source discovery and is hashed and deleted after a verified `go` or rollback.

Set `AGENT_CREW_SWAP_PRECHECK` to the owner-state command and its arguments; the wrapper appends `PROJECT` and requires a zero exit before entering any swap step. A failed check prints its output and stops. An unset command refuses the swap; `AGENT_CREW_SWAP_PRECHECK=none` explicitly skips this check. Every executed or explicitly skipped precheck writes an atomic JSON receipt before the swap step continues. Receipts default to `~/.agent_crew/PROJECT/swap-precheck-receipts`; set `AGENT_CREW_SWAP_RECEIPT_DIR` to choose another directory and `AGENT_CREW_SWAP_ACTOR` to identify the actor. `go --actor NAME` writes that same name to the precheck receipt and uses it for T0 verification; only `go` requires a simple actor name. A receipt write failure refuses the swap. The script reads port and database paths from `~/.agent_crew/PROJECT/state.json`. Keep runtime STOP paused throughout. A healthy rollback uses the same three commands with the previous full SHA. Do not run this against a live dispatcher without the owner's operational approval.

After spawning, `go` prints `STARTING` about every 30 seconds while the new process is alive but `/health` is unavailable. The default startup budget is 900 seconds; set `AGENT_CREW_SWAP_STARTUP_BUDGET_SECONDS` to override it. `GO_DONE` requires `/health` to report the requested project and full SHA with STOP paused. `go` then retains only `env.pre.sha256` and deletes the credential-bearing `env.pre.nul`; `post` does not need the dump.

## Hybrid memory prerequisite

Before a swap that sets `AGENT_CREW_MEMORY_BACKEND=hybrid`, set `AGENT_CREW_MEMORY_FLEET` to the fleet name used by fleet-scoped memory records (for the ADR-001 owner principles, `fleet`). Dispatch uses a task's `context.fleet` when supplied, otherwise this environment value. An unset fleet omits fleet-scoped records from the memory head; project-scoped records still apply. The `shadow_memory_retrieval` event and task context record the effective `fleet`, and dispatch logs a warning when it is empty. This setting enables the existing hybrid retrieval scope; the swap itself must follow the approval and pause procedure above.

Weighted hybrid retrieval is opt-in with `AGENT_CREW_MEMORY_FUSION_MODE=weighted`. Its independent vector arm scores every effective scoped row with a current vector for the active model, keeps the top 96 by cosine, and joins them with the lexical candidates. `AGENT_CREW_MEMORY_FUSION_WEIGHT` is the vector share of reciprocal-rank fusion; the Quota-tested lexical 1 : vector 2 setting is `0.6667`. The default mode remains `rrf`.

## If `go` fails after SIGTERM

A dead new process, expired startup budget, wrong project/SHA, or failed paused-STOP check means the swap is **not complete**. After the old listener stops, `go` automatically stops the new listener and relaunches the previous checkout identified by the captured `PYTHONPATH`, using the project-declared environment. It waits for the previous project/SHA and paused STOP, then exits nonzero with `ROLLBACK_OK served_sha=...`. The captured environment is hashed and deleted only after this verified rollback. Keep STOP paused and inspect the evidence in `~/.sev0-evidence/crew-swap-PROJECT-SHA7/` before any new attempt.

`ROLLBACK_FAILED served_sha=...` means the previous runtime was not verified. The captured `env.pre.nul` remains in the private evidence directory for manual recovery. Do not send work to an unverified listener. Use the following steps only after `ROLLBACK_FAILED`:

1. Read `health.pre.json` to get the previous full build SHA and verify that `~/alfred/runtime/agent_crew-PREV7` still has that exact clean HEAD. Read `state.json.pre`, `pause.json.pre`, `counts.pre.json`, and `server.log` in the evidence directory. Confirm `env.pre.nul` and `cwd.pre` exist. Do not print the environment dump; it contains provider credentials.
2. Inspect the port from `preflight.json`. If a wrong-build listener exists, confirm the queue remains empty and STOP paused, send it **SIGTERM only**, and wait for the port to close. If it does not close, stop and escalate; never use SIGKILL in this procedure.
3. Set `PROJECT` to the inspected project name, `EVIDENCE` to its evidence directory, `PORT` to the value in `preflight.json`, `PREV7` to the first seven characters of the verified previous full SHA, and `SWAP_REPO` to the absolute path of this repository. Relaunch the previous checkout using the project-declared environment, from the captured cwd:

   ```bash
   cd "$(cat "$EVIDENCE/cwd.pre")"
   python3 "$SWAP_REPO/scripts/spawn_from_env.py" "$HOME/.agent_crew/$PROJECT/fleet.env" "$HOME/.agent_crew/$PROJECT/cea.env" \
     "$EVIDENCE/server.rollback.log" "$PORT" "$HOME/alfred/runtime/agent_crew-$PREV7/src"
   ```

4. Check `/health` for the **full previous SHA** and paused STOP, and compare `SELECT status,count(*) FROM tasks GROUP BY status ORDER BY status` with `counts.pre.json`. If state or DB changed unexpectedly, stop and investigate before further work.
5. The backed-up `tasks.db.pre` is a consistent SQLite backup and `tasks.db.pre.sha256` verifies it. Restore it, `state.json.pre`, or `pause.json.pre` only after confirming no task or receipt was created since the backup, with the server stopped. These files are recovery evidence, not an automatic rewind of live state.

After manual recovery from `ROLLBACK_FAILED`, hash `env.pre.nul` into `env.pre.sha256` and delete the dump once the previous runtime is verified. The evidence root is `0700`; nonsecret metadata and backups remain for the incident record.

If `post` fails after `GO_DONE`, first check `/health`. `post` refuses unavailable health before opening the DB for counts. When the new runtime answers with the expected project/SHA and paused STOP, inspect the reported failure and rerun `post` after resolving it; the saved counts and pending-task evidence remain available and `post` does not need `env.pre.nul`. If the new runtime cannot be restored, use the separately approved runtime recovery procedure or start a fresh `preflight` against a healthy listener; `env.pre.sha256` cannot relaunch a process. Do not reconstruct credentials from the digest.

## Related docs

- [ADR-004 conformance gate, receipts, and coordinator-owned merges](conformance_gate.md)
