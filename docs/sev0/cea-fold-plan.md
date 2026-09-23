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

| Anchor (`queue.py`) | Role today | Under P6 |
|---|---|---|
| `:360-369` `_DDL_RUNTIME_STOP` (`id=1, epoch, paused, incident, note, updated_at`) | the #314 single row | `paused` → `state ∈ {ACTIVE, DRAINING, QUARANTINED, STOPPED}` + `reason` + `decision_id`; `epoch` kept; append-only `runtime_state_events` |
| `:603`, `:912-1001` boot reconcile (`pause.json` vs row, higher generation wins; fail-closed write `:993-1001`) | two stores reconciled at boot | `pause.json` = tighten-only input reconciled into the row (P6; fixture CX-P6b) |
| `:785` `_stop_dir`, `:787` `_read_stop_row`, `:799` `get_stop_epoch`, `:806` `set_stop_epoch` (epoch+1, `BEGIN IMMEDIATE`), `:830` `resume_stop` (generation CAS) | read/transition API | `RuntimeStateProvider.current()`; transitions gain `who` (P6 table: anyone tightens, owner loosens) |
| `:873` `_pausejson_active` (additive, fail-closed) | second gate-time store | removed from gate time (§11.2 #13 → T3) |
| `:889` `_stop_active_in_txn`, `:902` `_stop_active_precheck` | the gate predicate | becomes `validate_*` reading the one row |
| gates: enqueue `:1034-1038`, dequeue `:1326-1330`, cascade outbox `:1518-1529` (`_suppressed` `:1521`), `external_op_reserve` `:1856`, discuss `:2069` | STOP linearisation points | the same transactions host the T3 call points (P2 table "where") |
| `server.py:4679-4686` `/health.stop` | exposes `{epoch, paused, incident}` | `/health.runtime_state` (fixture CX-4j asserts `QUARANTINED`) |
| `cli.py:1502`, `:1533` pause/resume | mirror `pause.json` from the DB epoch | unchanged direction (DB is the linearisation point) |

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

## §P Provenance

- Provider Claude, model `claude-fable-5-1`, role implementer (`agent_override: claude`), task `sev0-cea-lineage-prep-r1` on :8105, branch `sev0/cea-lineage`.
- Read-only inputs: agent_crew `b574308`, `5efea31`, `4c123fc`, `37cb8af`, `846d13c`, `addc29e`, `4df04aa`, `b46bda4`, `3b8598f`, `9c90da1` (git objects); alfred `6cbce56` (E11 ADR), `sev0/e10-claude-redteam` E10 report + repro; `GET /tasks/sev0-e10-codex-challenge-r1` and `GET /tasks/sev0-cea-lineage` on :8105 (read-only, 2026-09-23).
- Merge trials: temporary detached worktree under `/tmp`, cherry-pick only, removed; no branch other than `sev0/cea-lineage` was created or moved. No live server, DB, GitHub or Telegram mutation.
