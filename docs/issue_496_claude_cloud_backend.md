# #496 — `claude_cloud` execution backend

Cloud dispatch backend for the existing Agent Crew implement→review→fix
lifecycle. Launches an eligible task via the Claude Code CLI's `--cloud`
surface instead of a local tmux pane. Opt-in, default OFF
(`AGENT_CREW_CLOUD_ENABLED`).

## Phase 0 — reuse-first audit

| Seam | Reused as-is | Notes |
|------|---------------|-------|
| Provider/dispatch abstraction | `role_mapping.py` (`role_agents.<role> = "claude_cloud"`) | Already provider-string-agnostic; zero changes needed there. |
| STOP/pause gate | `TaskQueue.dequeue()` | `dispatch_cloud_for_role` calls it directly — same fail-closed pause check as the tmux path. |
| CEA admission gate | `TaskQueue.record_dispatch(channel=..., ...)` | New `channel="claude_cloud"` value in the existing `tmux_pane/claude_p/codex_exec/gemini_cli/api` vocabulary. `AdmissionRefused` handled identically to `server._try_push_next`. |
| Task result schema | `protocol.TaskResult` / `_VALID_RESULT_STATUSES` | The five cloud outcomes map onto the *existing* five statuses (`completed`, `blocked`, `needs_human`, `failed`; `PR_READY` is `completed`+`pr_number`) — no new enum. |
| Review→fix→re-review loop | `pipeline.auto_enqueue_review`, `pipeline.review_head_status`/`review_is_current`, `AGENT_CREW_REVIEW_FIX_MAX_ROUNDS` | Called directly, unmodified. A discovered PR enters the SAME cascade a local provider's result would. |
| Fallback/quota handling | `pipeline.auto_fallback_failed_task` | Any cloud dispatch or continuation failure routes through the same rate-limit-shaped fallback local providers use — no new fallback policy. |
| Session lineage | `TaskQueue.record_attribution` / `get_attribution` (`task_attribution.provider_session_id`), `context.prev_task_id` chain via `pipeline._lineage_root_task_id` | Cloud session id persisted in the existing column; a fix task resolves its ORIGINAL cloud session by walking the same lineage chain the review/fix cascade already walks. |
| PR discovery | `github.branch_has_pr`, `github.pr_state`, `github.pr_head_sha` | Reused; extended with two small read-only additions (below). |
| Telemetry | `telemetry.TaskTelemetry` | One additive `Optional[str] = None` field, `execution_policy` (`"fresh_cloud"` / `"fresh_cloud_resume"` / `None`). |

### Genuinely missing seams (added, minimal, documented — not invented policy)

- **`github.pr_number_for_branch`** — `branch_has_pr` only returns a bool;
  the review cascade needs the actual PR number. Mirrors `branch_has_pr`'s
  exact `gh pr list` call, fails closed (`None`) instead of `branch_has_pr`'s
  fail-open `True` — "unknown" must never be read as "PR exists" here.
- **`github.branch_head_commit_message`** — reads a remote branch's HEAD
  commit message via `gh api repos/{repo}/commits/{branch}`. Needed because
  `ALREADY_FIXED` / `BLOCKED_FOR_CLOUD` / `NEEDS_DECISION` / `FAILED` do not
  require a PR (required behavior #2), so PR discovery alone cannot observe
  them.
- **`TaskQueue.count_in_progress_by_dispatch_channel` /
  `list_in_progress_by_dispatch_channel`** — no existing helper counted
  in-progress tasks by `dispatch_channel`; added following the exact style of
  `has_in_progress`.
- **No headless Cloud session status read exists** on the installed CLI
  (2.1.282; confirmed in the issue's own comment thread, checkpoint
  5880333100: `claude agents --json --all` lists only local sessions).
  Completion is observed **only** via GitHub (a PR, or a branch's HEAD
  commit message) — `reconcile_cloud_dispatch` / `reconcile_all_cloud_tasks`.
  A true `ALREADY_FIXED` that touches nothing and pushes no branch is **not
  observable** this way; this is an acknowledged gap in the parent system,
  not something this PR invents a workaround for.
- **Wiring reconciliation into a periodic trigger is left to the
  operator/coordinator.** `reconcile_all_cloud_tasks` contains no polling
  loop of its own — it is meant to be called from whatever already exists
  (cron, `crew triage --watch`, a future `crew` subcommand). Adding that
  wiring here would be a new scheduler, which is out of scope.
- **The exact CLI resume syntax for same-session fix continuation**
  (`claude --cloud <session_id> "<prompt>"`) is inferred from the single
  observed launch, not confirmed by the coordinator. `dispatch_cloud_for_role`
  gates every launch (fresh or resume) through the same fail-closed output
  parser and routes any resume that doesn't parse through the existing
  fallback — it never silently claims a resume worked.
- **Watchdog reaping of a stalled cloud dispatch** — `server._watchdog_tick`
  resolves a pane per in-progress task (`_resolve_pane_for_row`); a
  `claude_cloud` dispatch has no pane, so it is not currently reaped by the
  existing idle-timeout watchdog. Out of scope here; flagged for a follow-up
  decision rather than bolted on speculatively.

## What this PR does NOT do

No new orchestrator, scheduler, review pipeline, quota system, deploy path,
or memory system. No merge/deploy calls anywhere in `claude_cloud.py`
(mechanically asserted by a test). No change to the fleet-wide
persistent-session ADR (`docs/runtime_swap.md` untouched) — this backend's
default is explicitly a **fresh** Cloud session per task. No `runtime_swap`
or CEA authority code touched beyond the existing `record_dispatch` call
site every other backend already goes through.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `AGENT_CREW_CLOUD_ENABLED` | off | Opt-in kill switch. |
| `AGENT_CREW_CLOUD_MAX_CONCURRENCY` | `3` | Max concurrent `claude_cloud` in-progress dispatches. |
| `AGENT_CREW_CLOUD_CLI_PATH` | `claude` | Override the CLI binary path. |

An operator additionally opts a specific role in via
`role_agents.<role> = "claude_cloud"` (role_mapping.py) — by default no role
resolves to it, so existing local Claude/Codex/Gemini dispatch is unchanged.
