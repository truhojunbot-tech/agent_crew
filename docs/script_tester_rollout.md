# Script tester rollout (#772)

PR #774 adds a provider-free test stage. Existing project state files can still
explicitly select a provider tester, so merging the code alone does not switch
those projects. Changing a project's `state.json` is a separate rollout action;
its server must restart to load the new role mapping.

## Inventory before writing

Run the migration in read-only mode and review **every** project's old and new
tester, including unchanged projects:

Set `test_timeout_s` to at least 1.5 times the measured full suite duration
for each project. Pass `--test-timeout-s` and `--test-command` when planning
and repeat the same options when applying; they are included in the guarded
report hash. Omit either option to leave that state field unchanged.

```bash
PYTHONPATH=src python3 scripts/migrate_script_tester.py \
  --crew-home "$CREW_HOME" > /tmp/script-tester-plan.json
```

The JSON report includes the path, old/new tester, action, state-file digest,
and a report `sha256`. To retain a provider tester intentionally, add
`"tester_provider_opt_in": true` beside an explicit
`"role_agents": {"tester": "gemini", ...}` before generating the report.
Without that opt-in, explicit and legacy provider tester mappings are planned
for migration. Legacy `roles` and `worktrees` stay unchanged for checkout
routing.

After reviewing the complete report, apply **only that exact report**:

```bash
PYTHONPATH=src python3 scripts/migrate_script_tester.py \
  --crew-home "$CREW_HOME" --apply \
  --accept-report-sha256 "$(python3 -c 'import json; print(json.load(open("/tmp/script-tester-plan.json"))["sha256"])')"
```

Apply refuses a stale report hash, writes each changed state file atomically,
and preserves the file mode. Review and coordinate the server restarts
separately. Rollback is to restore the previous state-file tester assignments
and restart, or revert the code.

## Test command environment

The script runs with a small environment: `PATH`, `HOME`, locale, timezone,
temporary directory, a Python user-site path, and `PYTHONPATH` pointing at the
checkout under test. It never inherits `AGENT_CREW_*` or provider credentials
from the dispatcher. A project can add literal non-secret variables through
`state.json.test_env`, for example `{"PROJECT_TEST_FLAG": "1"}`. Keys containing
`TOKEN`, `SECRET`, `PASSWORD`, `API_KEY`, `CREDENTIAL`, `AUTH`, or `PRIVATE_KEY`
are rejected, as are `AGENT_CREW_*` and `PYTHONPATH`. The base-result cache
includes the effective test environment and timeout in its identity.
