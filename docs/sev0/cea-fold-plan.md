# SEV-0 CEA fold plan — b574308 anchors and lane rebase analysis

- **Kind:** `discovery` (anchors and measured merge trials) + `proposal` (fold points). Doc only; nothing here changes code.
- **Task:** `sev0-cea-lineage-prep-r1` (implement, `agent_override: claude`, :8105), step 3. Branch `sev0/cea-lineage` from `b574308` (PR #375 head).
- **Contract:** alfred `sev0/e11-adr-draft` `evidence/sev0-p0/E11-ADR-DRAFT.md` @ `6cbce56` ("the ADR"; Π P1–P7, §2.2 ingresses, §3, §7, §8, §11).
- **Base read:** agent_crew `b574308` (= `5efea31` + `36e182d` + `b574308`; files vs `5efea31`: `pipeline.py`, `risk_tier.py`, `server.py`, 2 tests). Every line number below is `git show b574308:<file>` unless a lane commit is named.
- **Freeze rule (owner 11354):** this document records *where* each fold lands and *what conflicts*; it resolves nothing. Interface candidates are in `src/agent_crew/cea/` (typed only).

## 1. Enqueue paths at b574308 (every way a row reaches `QUEUED`)

P1: after the fold, the raw INSERT is private to `enqueue_with_receipt`, and each row below is a §7 adapter that calls the engine. "Direct SQLite" rows are the ones §2.2 does not name and that bypass the server today.

| # | Ingress (§2.2) | Anchor at b574308 | What it does today | Fold |
|---|---|---|---|---|
| E1 | direct `POST /tasks` | `server.py:4773` `post_task`; raw insert `q().enqueue(task)` `:4815`; push `_try_push_next(role)` `:4852`, `_try_push_discuss` `:4846` | no gate; `201` unconditionally (E10 4a; CXC-3) | engine at the endpoint; adapter `direct`/`manual`/`coordinator`/`cron` by credential (§6.5) |
| E1a | #294 duplicate-in-flight advisory | `server.py:4797-4836` (`task_issue_number` `:4802`, `active_tasks_for_issue` `:4805-4806`, warning `:4830-4836`); helper `watch.py:554` | lexical, advisory, never blocks | **REMOVE** (§11.1 row 14; P4 `intent_hash` index) — fixture CXC-1 |
| E2 | MCP | `mcp_server.py` has **no create path**: `get_next_task` `:109` (claim via `queue.dequeue`), `get_next_discuss_task` `:138`, `submit_result` `:146` → `_cascade_stage` `:285-304` (calls pipeline `auto_enqueue_review/_test/_fix` `:292/:295/:304`), `cancel_task` `:341` | MCP is a claim + result + cascade ingress, not an admission ingress | claim → T3 `validate_claim`; result → T3 `validate_result`; cascades → E5 |
| E3 | CLI (`crew run`, `crew enqueue`) | `cli.py:3275` `enqueue()`: HTTP `POST /tasks` `:3329` when a port is known, **else direct SQLite** `TaskQueue(db).enqueue(TaskRequest(**payload))` `:3344` | the fallback writes QUEUED rows with no server, no push, no gate | HTTP path = E1 adapter `manual`; the direct-SQLite fallback must go (P1 mechanical guarantee) |
| E3a | loop client (`crew run`) | `loop.py:10-33` `_post_task_http` (POST); `enqueue_implement` `:51-61` (`queue.enqueue` `:61` when `port=0`), `enqueue_review` `:89-128` (`:128`), `enqueue_test` `:131-154` (`:154`), `enqueue_implement_with_feedback` `:228` | same split: HTTP when a port is given, else direct SQLite | as E3 |
| E3b | discuss panel | `discussion.py:9-36` `enqueue_panel_tasks` (`_post_task_http` or `queue.enqueue` `:36`); claim `queue.py:2059` `dequeue_discuss_for_agent` (stop re-check `:2069`); push `server.py:4846` | same split | discuss is `work_class=ops`; adapter `manual`/`coordinator` |
| E4 | pipeline (transport-agnostic cascades) | `pipeline.py:588` `auto_enqueue_fix` (`external_op_reserve` `:726`, `queue.enqueue` `:801`); `:990` `auto_enqueue_review` (`:1213`); `:1231` `auto_enqueue_test` (`:1334`); `:1369` `auto_fallback_failed_task` (`:1526`); `:1350` `resume_tier3_gate` | in-process inserts, no gate; STOP checked via `runtime_stop` in `queue.enqueue` | cascade adapter: engine called in-process with `parent_receipt_id` (§8), `work_class` per successor |
| E5 | server cascades on `/result` | `server.py:4324` `_auto_enqueue_review` (→ E4), `:4351` `_auto_enqueue_test` (**own** `q().enqueue` `:4399`), `:4428` `_auto_enqueue_fix`, `:4557` `_auto_merge_pr`; decision block `:5079-5262` | `coordinator_managed` **skips** review `:5082-5091`, test `:5237-5241`, fix `:5247-5254`, merge `:5235` / `:5257` | **REMOVE** the four suppression branches (§7.2, §11.1 row 10; fixture CX-4b); the flag becomes provenance with a named coordinator |
| E6 | retry / fallback | `server.py:4464` `_auto_retry_failed_task` (`q().enqueue(retry_req)` `:4538`); `:4618` `_auto_fallback_failed_task` (→ pipeline `:1369`) | new rows with a retry suffix, no gate | P4 retry rule: same receipt iff B unchanged and `attempt < max_attempts`; else re-admit (adapter `retry`) |
| E7 | recovery | `server.py:2373-2402` `_requeue_orphans` (`tq.requeue` `:2401`, runs in `lifespan` `:2430` when the dispatcher is enabled); other `requeue` call sites `:2521, :2528, :2710, :2720, :3355, :4067, :4246, :4280` | in_progress → pending with no re-validation | P7 recovery: every non-terminal receipt re-validated before any claim; `requeue` becomes `validate_claim`-gated |
| E8 | STOP replay | `server.py:5270-5310` `/admin/replay-suppressed` (outbox drain); `:2456-2471` `PausedError` handler (outbox reopen); `queue.py` cascade outbox DDL `:376` | replays stored result bodies into E5 after resume | replay = new engine calls under current B (P4 "no in-memory replay bypasses the validator") |
| E9 | watchdog | outside agent_crew (alfred A+); in-server `_watchdog_loop` `server.py:3150` only pushes reminders `:3072` | — | adapter `watchdog` (§2.2) |
| E10 | risk-tier decision | `server.py:59` import; `:3691-3695` (`test_scope` decision when `risk_tier_enforcement_enabled()`); `pipeline.py:514` `_record_risk_tier_shadow`; `risk_tier.py` | second risk decision beside quota-core | **REMOVE** as a decision (§11.1 row 13, O10); J7 reads the snapshot `review_test_matrix` — fixture CXC-1 |

**Claim points (T3 `validate_claim`):** `queue.py:1300` `dequeue` (precheck `:1320`, in-txn STOP `:1326-1330`); callers `server.py:2596` (`_try_push_next`), `:4231` (`_dispatcher_loop`), `:4871` (`GET /tasks/next` `:4859`); `mcp_server.py:109`; discuss `queue.py:2059/2069`.

**Dispatch points (T3 `validate_dispatch`, then T6 G_DT):** pane push `push_fn(...)` `server.py:2781` and `:2877` (guarded by `_guard_task_existence` `:2476`, #362); one-shot spawn `:3285` `_dispatch_task` (target resolution `:3253`). Cascade-side suppression decision: `queue.py:1521` `_suppressed` (in `submit_result`'s txn) read back at `server.py:5056`.

**Execute start (T3 `validate_execute_start`):** one-shot — inside `_dispatch_task` before `subprocess` spawn (`:3285+`); pane — new `POST /tasks/{id}/start {nonce}` (no anchor today; P2).

**`/result` (T3 `validate_result`, then T5 G11):** `server.py:4888` `submit_result`: context/task read `:4893-4895`; STOP read `:4896-4902`; artifact gate `:4903-4919` (skip branch "not applied — dispatch base absent" `:4906-4907`); #268 PR mismatch `:4926`; prior status `:4934`; `q().submit_result` `:4937` (queue `:1415`, outbox `:1508-1529`); outbox `_suppressed` `:5056-5063`; artifact held `:5064`; discuss `:5068`; failed → fallback/retry `:5075-5078`; cascades `:5079-5262`.

## 2. `runtime_stop` (P6 store to generalise, not duplicate)

| Anchor (`queue.py`) | Role today | Under P6 | Status |
|---|---|---|---|
| `:360-369` `_DDL_RUNTIME_STOP` (`id=1, epoch, paused, incident, note, updated_at`) | the #314 single row | `paused` → `state ∈ {ACTIVE, DRAINING, QUARANTINED, STOPPED}` + `reason` + `decision_id`; `epoch` kept; append-only `runtime_state_events` | **DONE** (step 1, `4b62f32`): additive ALTERs + backfill `paused=1 → STOPPED`; `runtime_state_events` with UPDATE/DELETE triggers |
| `:603`, `:912-1001` boot reconcile (`pause.json` vs row, higher generation wins; fail-closed write `:993-1001`) | two stores reconciled at boot | `pause.json` = tighten-only input reconciled into the row (P6; fixture CX-P6b) | **PARTIAL**: the reconciled verdict now writes `state` and appends an event; the #314 higher-generation rule is unchanged, so a higher `pause.json` generation can still resume. Gate-time `pause.json` remains additive (tighten-only) and is folded into `effective_state` |
| `:785` `_stop_dir`, `:787` `_read_stop_row`, `:799` `get_stop_epoch`, `:806` `set_stop_epoch` (epoch+1, `BEGIN IMMEDIATE`), `:830` `resume_stop` (generation CAS) | read/transition API | `RuntimeStateProvider.current()`; transitions gain `who` (P6 table: anyone tightens, owner loosens) | **DONE** (step 1): `get_runtime_state()` + `transition_runtime_state(to, who=, decision_id=)`; `set_stop_epoch`/`resume_stop` keep working and now record `who='legacy:*'` events — see the gap note below |
| `:873` `_pausejson_active` (additive, fail-closed) | second gate-time store | removed from gate time (§11.2 #13 → T3) | **PARTIAL**: still read at gate time, but only as a tightening (`_runtime_state_in_txn`). Removing it outright regresses global/direct pause on a live server; it goes when T3 owns the gate (step 2) |
| `:889` `_stop_active_in_txn`, `:902` `_stop_active_precheck` | the gate predicate | becomes `validate_*` reading the one row | **DONE** (step 1) as `_runtime_state_in_txn(conn) != 'ACTIVE'` — DRAINING and QUARANTINED now gate enqueue/claim, which the boolean could not express. `validate_*` wiring is step 2 |
| gates: enqueue `:1034-1038`, dequeue `:1326-1330`, cascade outbox `:1518-1529` (`_suppressed` `:1521`), `external_op_reserve` `:1856`, discuss `:2069` | STOP linearisation points | the same transactions host the T3 call points (P2 table "where") | step 2 |
| `server.py:4679-4686` `/health.stop` | exposes `{epoch, paused, incident}` | `/health.runtime_state` (fixture CX-4j asserts `QUARANTINED`) | **DONE** (step 1): `/health.runtime_state` = `{state, effective_state, epoch, reason, decision_id, incident, pause_json_tightening, read_failed}`; `/health.stop` kept for #314 callers |
| `cli.py:1502`, `:1533` pause/resume | mirror `pause.json` from the DB epoch | unchanged direction (DB is the linearisation point) | unchanged |

## 3. Lane rebases onto b574308 — measured, not resolved

Method: throwaway detached worktree at `b574308`, `git cherry-pick -x` per lane commit, git 2.34.1, **textual only, no tests run**, worktree removed afterwards (2026-09-23, this task). Each lane's own base is `5efea31`; `b574308` is 2 commits ahead of it (`36e182d`, `b574308`), touching `pipeline.py`, `risk_tier.py`, `server.py` (5 lines), 2 tests.

| Lane | Commits | Files (non-test) | Alone onto b574308 |
|---|---|---|---|
| G11 (#374) | `37cb8af` → `846d13c` | `instructions.py`, `mcp_server.py`, `pipeline.py` (+209/+93), `protocol.py`, `server.py` `43-52` (imports), `4898-4925` (`/result` artifact gate → `artifact_gate_applies`/`verify_task_artifact`), `4936-4945` | **clean** |
| G_DT | `addc29e` → `4df04aa` | `server.py` `1751-1863` (pane process-kind helpers), `2368-2376`, `2484-2634` (push refusal + `defer_push_delivery`), `2526-2681`, `2919`, `3047`, `3066`, `4687` (health); `queue.py` `dequeue(..., *, skip_deferred)` `1297-1395`, `defer_push_delivery` (+ before `requeue` `:1923`), `dequeue_discuss_for_agent(..., *, skip_deferred)` `2106-2140`; `pyproject.toml`, `tests/conftest.py`, docs | **clean** |
| G12 | `b46bda4` → `3b8598f` → `9c90da1` | `queue.py` DDL `148-203` (`_EXEC_STATE_COLUMN_TYPES`, `task_exec_events`), migration `637-647`, `_claim_build` `194-210`, `dequeue(..., *, claimed_via)` `1371-1376` + claim record `1463-1476`, end-lease in `submit_result` `1569-1579`, `get_exec_state` (+ before `requeue`), `_append_exec_event_on/_record_claim_on/_record_end_on/record_dispatch/record_heartbeat` (+2002-2133), `cancel` `2153-2166`, discuss `2288-2335`, `expire_stale` `2599-2614`, watchdog `3111/3147`; `server.py` `997-1010`, `2593-2600` (`dequeue(role, claimed_via="tmux_push")`), `2782-2791` / `2877-2889` (`record_dispatch` on push), `2827-2837`, `2930-2942`, `3282-3312` (`_process_heartbeat`), `3972-4018` (`record_dispatch` on spawn), `4226-4296` (`claimed_via="dispatcher"`), `4866-4908` (`GET /tasks/next` `claimed_via="http_poll"` + `record_dispatch`; `GET /tasks/{id}` `execution`), `5372-5405`; `mcp_server.py`; `tests/fixtures/pre_g12_tasks.db`; docs | **clean** |
| E8 | `4c123fc` | tests + doc only | **clean** (already on this branch as `c31541c`) |

**Pairs:**

| Order | Result |
|---|---|
| G11 → G12 | clean |
| G_DT → G11 | clean |
| E8 → G11 → G_DT | clean |
| G_DT → G11 → **G12** | **CONFLICT** at `b46bda4` (G12 1/3): `queue.py`, one region — both lanes insert a new method immediately before `def requeue` (`queue.py:1923`): G_DT `defer_push_delivery`, G12 `get_exec_state`. `3b8598f` (G12 2/3) would then add the regions listed in the next row. |
| G12 → G_DT | `addc29e` clean; **CONFLICT** at `4df04aa` (G_DT 2/2): `queue.py` 4 regions — top-of-file import (`contextlib` vs G12's import), `dequeue` signature + docstring (`skip_deferred` vs `claimed_via`, both keyword-only), `dequeue_discuss_for_agent` signature (same), the pre-`requeue` method insertion; `server.py` 2 regions — the push-path `q().dequeue(role=role, ...)` call (`skip_deferred=True` vs `claimed_via="tmux_push"`) and the push-refusal / `defer_push_delivery` block vs G12's `record_dispatch` on the same push path. |

**What the G12 ↔ G_DT conflict is (not resolved here):**

1. *Mechanical:* two keyword-only parameters on `dequeue` / `dequeue_discuss_for_agent`; two imports; two methods inserted at the same spot; one call site needing both arguments. Any resolver can merge these.
2. *Semantic — belongs to the fold, not to a rebase:* on the tmux push path G_DT may **refuse** delivery (foreign pane / no agent CLI) and back the task off, while G12 **records** a dispatch with a pane lease. Which runs first decides whether a refused push leaves a `dispatched_at`/`lease_owner` on the row. Under §11.2 the order is fixed by the architecture: T3 `validate_dispatch` → T6 G_DT topology check → deliver → G12 event with `receipt_id`; a refused push is an event (`dispatch_refused`) and never a lease. O12 assigns who resolves this and requires re-review after the rebase.

**ADR merge order (Appendix A):** (1) G15 `b574308` (this base) → (2) E8 + §12.2 fixtures (this branch: `c31541c`, `4c69c3a`) → (3) G12 + Codex #6 fixes → (4) engine + runtime state + P2a broker → (5) G11, G_DT rebased → (6) remove `risk_tier.py` decision + #294 advisory → (7) E8 green. G11 is order-independent (clean against every combination above); G12 before G_DT matches the ADR order and puts the semantic decision in step (5) where it is re-reviewed.

## 4. Fold points per lane (where each lands after the validator)

| Lane | ADR row | Lands at | Change in meaning |
|---|---|---|---|
| G11 `846d13c` | §11.1 row 10 (T5) | `/result` after `validate_result` (`server.py:4903-4919` today) | required reviewer/tester come from the receipt (`required_reviewer/tester`); the "not applied — dispatch base absent" skip (`:4906-4907`; G11 keeps it via `artifact_gate_applies`) becomes **FAIL** — fixture CXC-6a |
| G_DT `4df04aa` | §11.1 row 11 (T6) | after `validate_dispatch` on the push path (`server.py:2596-2781`) | topology/foreign-pane check only; the #362 "unknown task" refusal (`_guard_task_existence` `:2476`) is subsumed by "no receipt ⇒ no dispatch" |
| G12 `9c90da1` | §11.1 row 9 (T3 evidence) | `task_exec_events` + `authorization_receipts` under one append-only trigger; `cancel` (`queue.py:1935`) records the terminal event and clears the lease in one txn; `GET /tasks/{id}` (`server.py:4880`) redacts pid/pane/lease | events carry `receipt_id` — fixtures CXC-6b, CXC-6c |

## 5. Findings while anchoring (`discovery`)

- **Three direct-SQLite enqueue paths bypass the server entirely** at b574308: `cli.py:3344`, `loop.py:61/128/154` (port 0), `discussion.py:36`. §2.2 lists "manual operator (`crew run`, curl)" as one ingress via the endpoint; these fallbacks are a fourth, unlisted way into `QUEUED`. The I2 static test must enumerate `queue.enqueue` call sites across `cli.py`, `loop.py`, `discussion.py`, `pipeline.py`, `server.py`, not routes alone.
- **MCP has no admission ingress** (no create tool), so §2.2's route table is complete for creation, but MCP is a *claim* ingress (`get_next_task` → `dequeue`) and a *result* ingress with its own cascade copy (`_cascade_stage`), so it has three T3 call sites, not zero.
- **`_auto_enqueue_test` in `server.py` (`:4351-4399`) has its own `q().enqueue`**, separate from `pipeline.auto_enqueue_test` (`:1231`); the HTTP and MCP test cascades are two code paths for one invariant (same class as §11.3).
- **`_requeue_orphans` runs before the dispatcher loop starts** (`server.py:2430`) with no re-validation — exactly the P7 recovery point; it is the natural first caller of `validate_claim` on restart.

## 6. Step 1 — what landed, what did not (`result`)

Task `sev0-cea-lineage-s1-state-validator`, commit `4b62f32` on `sev0/cea-lineage`
(base `d073a59`). Contract frozen at alfred `6cbce565`; receipt schema copied
byte-identically from alfred `e1063eb` (blob `41e7ebf`, asserted by a test).

| Item | State | Where |
|---|---|---|
| P6 state + epoch + reason + decision_id on the `runtime_stop` row | **done** | `queue.py` `_DDL_MIGRATE_RUNTIME_STOP_P6`, `_DDL_BACKFILL_RUNTIME_STOP_STATE` |
| append-only `runtime_state_events` (+ UPDATE/DELETE triggers) | **done** | `queue.py` `_DDL_RUNTIME_STATE_EVENTS*` |
| transition rules / who-may-transition | **done** | `queue.transition_runtime_state`, `_transition_refusal` |
| `/health.runtime_state` | **done** | `server.py` `/health` |
| `authorization_receipts` = the frozen schema, append-only | **done** | `cea/store.py` |
| `dispatch_nonces` single-use | **done** | `cea/store.py` `mint_nonce`/`consume_nonce` |
| `tasks.receipt_id` (nullable) | **done** | `cea/store.py` `_DDL_MIGRATE_TASKS_RECEIPT_ID` |
| one validator: P3 table, P6 matrix, P7, O18, O20 | **done** | `cea/validator.py` `validate()` |
| the five validator call sites | **not started — step 2** | — |
| `tasks.receipt_id` `NOT NULL` + FK | **not started — step 2** | — |
| P4 unique partial index on `intent_hash` | **not started** | needs the "current state" view over the append-only rows; lands with the engine |

**Honest gaps in step 1** (they are not hidden behind a green test):

1. `set_stop_epoch(False)` and `resume_stop(...)` still loosen the runtime without
   an owner `decision_id`. They are the #314 CLI/fleet paths and breaking them
   would break `b574308`; they now record `who='legacy:set_stop_epoch'` /
   `'legacy:resume_stop'` events so the bypass is visible in the audit trail. The
   P6 authority rule is enforced on `transition_runtime_state`, which is the API
   step 2 routes the transition endpoint through.
2. `pause.json` is still read at gate time (as a tightening only). P6 wants it
   read at boot only; removing the gate-time read now regresses global/direct
   pause on a live server (the #314 reviewer note at `queue.py:_pausejson_active`).
3. The validator's signature check reads `signature.status`; there is no engine
   key yet (O3), so every receipt is `UNVERIFIED` and admissible only with a
   `downgrade_reason`. That is P2a's stated position, not an oversight.

**Verification** (`tests/test_cea_step1_state_and_validator.py`, 127 tests): schema
blob identity; the dependency-free fallback checker agreeing with `jsonschema` on
every fixture; trigger rejection of UPDATE/DELETE on both append-only tables;
migration idempotence over three opens of a copy of the committed fixture DB **and**
of a copy of the live `~/.agent_crew/agent_crew/tasks.db`; the P6 transition table
row by row; all 20 cells of the P6 enforcement matrix; every row of the P3 outcome
table; each O18 immediate-invalidation field against a one-minute-old receipt; and
the O20 window on both sides. No live server, DB or GitHub state was touched — the
live DB is copied into `tmp_path` and opened there.

## 7. Step 4b — what landed, what did not (`result`)

Task `sev0-cea-lineage-s4b-folds-acceptance-guards-r1`, branch `sev0/cea-lineage`,
base `ba1d71d` (4a's head). Contract frozen at alfred `6cbce565`.

### DONE in this step

| Item | Where | Evidence |
|---|---|---|
| FOLD-IN 3 P1 — RESULT requires that EXECUTE_START consumed the nonce | `cea/store.py` (`EXECUTE_START_CONSUMER`, `consumer_tag`, `consumed_by_execute_start`), `cea/validator.py` (`nonce_consumed_by`/`nonce_attempt`, `_nonce_started`, three RESULT refusals), `queue.py` (`start_execution` tags the spend; `submit_result` reads the nonce row), `server.py` (`/result` → 409), `mcp_server.py` (same refusal) | `tests/unit/test_sev0_cea_s4b_result_requires_start.py` (13 tests); step-1 suite updated (147); CEA unit files 190 passed / 31 xfailed |

The reproduction, run against `ba1d71d` in a throwaway copy of the tree before
the fix (`mode=test`, `start_execution` omitted): task `completed`, nonce
`used_at=None`, receipt `CLAIMED → CONSUMED` — never `RUNNING`. After the fix
the same sequence is refused `NONCE_NOT_STARTED`, nothing is written, and the
nonce stays unspent.

### Guard inventory as measured on this branch (ADR §11.2, item (g))

`discovery` — this is what the code contains today, not a claim that the count
is already at or under the ADR's ceiling of six.

| Guard | Implementation on this branch | Status |
|---|---|---|
| T3 validator (the five points) | `cea/callsites.py` — one `VALIDATOR`, five `gate_*` | the one decision surface |
| G11 artifact contract | `pipeline.artifact_gate_applies` + `/result`, `mcp_server.py:234` | present; **not yet** invoked *from* the validator at T5, and "dispatch base absent" still skips rather than FAILs — item (a), REMAINING |
| G_DT dispatch topology | `server._guard_agent_process`, `_guard_task_existence`, `queue.defer_push_delivery` | present; the #362 unknown-task refusal is **not yet** subsumed by "no receipt ⇒ no dispatch" — item (b), REMAINING |
| G12 execution events | `queue.record_dispatch`/`_record_claim_on`/`_record_end_on`, `task_exec_events` | present; `receipt_id` on the rows, `cancel()` single-txn terminal event, `GET /tasks/{id}` redaction and the append-only trigger — item (c), REMAINING (unverified here) |
| #294 lexical duplicate advisory | `server.py:5093` `active_tasks_for_issue` | **to remove** (§11.1 row 14) — REMAINING |
| risk-tier decision | `risk_tier.risk_tier_enforcement_enabled` read at `pipeline.py:981/1296/1542`, `server.py:3931` | **to remove as a decision** (§11.1 row 13, O10) — REMAINING |

### REMAINING after this step (nothing below was started)

Carried from 4a's s2b remainder:

1. Remove the #294 lexical duplicate advisory (`server.py:5093`).
2. Remove `risk_tier.py` as a decision; J7 reads the snapshot `review_test_matrix`.
3. Receipt-less legacy rows: not runnable under `enforce`, counted under `shadow`
   (the claim path already refuses them under `enforce` — `queue.py:2172`; the
   *report* under shadow is not built).
4. Rollout config: engine mode `off|shadow|enforce|test` **per project** (today
   the mode is process-wide, `AGENT_CREW_CEA_MODE`). Default `shadow` for live.
5. E8 §11 scenarios flipping from xfail to pass under `enforce` (31 xfail today).

This step's own list:

- (a) G11 invoked from the validator at T5; "dispatch base absent" ⇒ FAIL.
- (b) G_DT invoked after the validator at T6, topology only.
- (c) G12 `receipt_id` on `task_exec_events`, single-txn `cancel()`, public
  redaction, append-only triggers.
- (d) I1 property test (static exhaustiveness over adapters + generated fixtures
  through every adapter ⇒ identical `(decision, reason, intent_hash)`).
- (e) I2 static + dynamic (no receipt / CONSUMED / stale per O18 field /
  DRAINING, QUARANTINED, STOPPED / past the O20 window).
- (f) Permanent fixtures: E10 4a–4j, the Codex six, same-uid executor/caller —
  BLOCKED vs expected-red.
- (g) Guard count reduced to ≤ 6 with the removals above; the table in this
  section is the *starting* inventory, not the finished count.

## §P Provenance

- Provider Claude, model `claude-fable-5-1`, role implementer (`agent_override: claude`), task `sev0-cea-lineage-prep-r1` on :8105, branch `sev0/cea-lineage`.
- Step 4b (§7): provider Claude, model `claude-opus-5`, role implementer (`agent_override: claude`), task `sev0-cea-lineage-s4b-folds-acceptance-guards-r1` on :8105, branch `sev0/cea-lineage`, base `ba1d71d`, 2026-09-23. Read-only inputs: `GET /tasks/sev0-cea-lineage-s4a-merge-remainder-p1s` on :8105. The pre-fix reproduction ran in a throwaway copy of the tree under `/tmp` (removed); no live server, DB, GitHub or Telegram mutation.
- Step 1 (§6): provider Claude, model `claude-opus-5`, role implementer (`agent_override: claude`), task `sev0-cea-lineage-s1-state-validator` on :8105, same branch, base `d073a59`, 2026-09-23.
- Read-only inputs: agent_crew `b574308`, `5efea31`, `4c123fc`, `37cb8af`, `846d13c`, `addc29e`, `4df04aa`, `b46bda4`, `3b8598f`, `9c90da1` (git objects); alfred `6cbce56` (E11 ADR), `sev0/e10-claude-redteam` E10 report + repro; `GET /tasks/sev0-e10-codex-challenge-r1` and `GET /tasks/sev0-cea-lineage` on :8105 (read-only, 2026-09-23).
- Merge trials: temporary detached worktree under `/tmp`, cherry-pick only, removed; no branch other than `sev0/cea-lineage` was created or moved. No live server, DB, GitHub or Telegram mutation.
