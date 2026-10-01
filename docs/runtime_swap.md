# Crew runtime swap and recovery

Run `scripts/runtime_swap.sh PROJECT FULL_SHA preflight`, then `go`, then `post` with `AGENT_CREW_SWAP_CEA_ENV_FILE` pointing to a private file of `AGENT_CREW_CEA_KEY=VALUE` lines. The script reads port and database paths from `~/.agent_crew/PROJECT/state.json`. Keep runtime STOP paused throughout. A healthy rollback uses the same three commands with the previous full SHA. Do not run this against a live dispatcher without the owner's operational approval.

## If `go` fails after SIGTERM

A health timeout or build SHA mismatch means the swap is **not complete**. Keep STOP paused and do not send work to the listener. The script deliberately does not restart a process automatically after a failed provenance check; the operator must inspect the actual listener and decide whether a restart is safe. The preflight evidence is in `~/.sev0-evidence/crew-swap-PROJECT-SHA7/`.

1. Read `health.pre.json` to get the previous full build SHA and verify that `~/alfred/runtime/agent_crew-PREV7` still has that exact clean HEAD. Read `state.json.pre`, `pause.json.pre`, `counts.pre.json`, and `server.log` in the evidence directory. Confirm `env.pre.nul` and `cwd.pre` exist. Do not print the environment dump; it contains provider credentials.
2. Inspect the port from `preflight.json`. If a wrong-build listener exists, confirm the queue remains empty and STOP paused, send it **SIGTERM only**, and wait for the port to close. If it does not close, stop and escalate; never use SIGKILL in this procedure.
3. Set `EVIDENCE` to the inspected evidence directory, `PORT` to the value in `preflight.json`, `PREV7` to the first seven characters of the verified previous full SHA, and `SWAP_REPO` to the absolute path of this repository. Relaunch the previous checkout using the captured environment and the same private CEA env file, from the captured cwd:

   ```bash
   cd "$(cat "$EVIDENCE/cwd.pre")"
   python3 "$SWAP_REPO/scripts/spawn_from_env.py" "$EVIDENCE/env.pre.nul" "$AGENT_CREW_SWAP_CEA_ENV_FILE" \
     "$EVIDENCE/server.rollback.log" "$PORT" "$HOME/alfred/runtime/agent_crew-$PREV7/src"
   ```

4. Check `/health` for the **full previous SHA** and paused STOP, and compare `SELECT status,count(*) FROM tasks GROUP BY status ORDER BY status` with `counts.pre.json`. If state or DB changed unexpectedly, stop and investigate before further work.
5. The backed-up `tasks.db.pre` is a consistent SQLite backup and `tasks.db.pre.sha256` verifies it. Restore it, `state.json.pre`, or `pause.json.pre` only after confirming no task or receipt was created since the backup, with the server stopped. These files are recovery evidence, not an automatic rewind of live state.

On a successful `post`, the script retains `env.pre.sha256` and deletes the secret-bearing `env.pre.nul`. If `go` fails, keep the dump in its `0700` evidence directory until recovery is verified, then delete it manually. The evidence root is also `0700`; nonsecret metadata and backups remain for the incident record.
