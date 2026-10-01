# SEV-0 CEA guard inventory — ADR §11.2 against the code

`result` — grep-based inventory of every go/no-go judgement, matcher and ack
ledger left in `src/agent_crew/`. It counts what the ADR counts in §11.2.

- **Contract:** alfred `sev0/e11-adr-draft` @ `6cbce565`, `evidence/sev0-p0/E11-ADR-DRAFT.md` §11.2 and §11.3. There are 19 guards today and the target is 6 or fewer.
- **Code read at:** agent_crew `bd58092` on branch `sev0/cea-lineage-s4d`. The 4c lane is changing `queue.py`, `server.py`, `validator.py` and `cea/*.py` on `sev0/cea-lineage` at the same time, so the line numbers below will move. Run the greps again on the merged head before quoting any of them.
- **How it was counted:** each guard was searched by its name or its effect. The ADR counts one implementation once, however many call sites it has. Guards that live only on the alfred side (§11.2 rows 1–8, 18 and 19: `admitted_trigger`, `ssot_preflight`, the ack JSONs) are out of scope for this repo and are listed only so the table is complete.

## 1. Target components (T1–T6) — present

| T | Implementation | Where (bd58092) |
|---|---|---|
| T1 | Engine `authorize()` | `cea/engine.py:440` `AuthorizationEngine.authorize` (embedded, `mode=test`/`shadow`); `cea/service.py` (enforce, separate process) |
| T2 | E4 capability lookup — the only matcher | `cea/input_providers/capability.py:27` `lookup`; consumed at `cea/engine.py:1197` `_matched` |
| T3 | Receipt validator — one instance, five call sites | `cea/validator.py:347` `validate`; `cea/callsites.py` `VALIDATOR`; its gates are called from `queue.py:1921` (enqueue), `:2135` (claim), `:3166` (dispatch), `:3264` (execute start) and `:2552` (result) |
| T4 | Snapshot producer ack ledger | alfred side only. There is no ack ledger in agent_crew src: `grep policy_ack.json\|ssot_decision_ack.json` finds nothing (see CXC-1 in `tests/unit/test_sev0_cea_permanent_fixtures.py`) |
| T5 | G11 result-contract check | `pipeline.py:200` `artifact_gate_applies` |
| T6 | G_DT pane-topology delivery precondition | `queue.py:3346` `defer_push_delivery`; `server.py:2632` `_guard_task_existence` |

## 2. Current rows from §11.2 — removed vs remaining

| §11.2 # | Guard | Status in agent_crew src | Evidence |
|---|---|---|---|
| 11 | #294 duplicate-in-flight advisory (lexical) | **REMOVED** | `grep -n "duplicate.in.flight\|DUPLICATE_IN_FLIGHT"` finds nothing. The P4 `intent_hash` index replaces it; `DUPLICATE_INTENT` comes from the engine (fixture CX-4c) |
| 12 | `runtime_stop` STOP check | **REMAINS, still a separate claim.** The P6 row is read by T3, but `_stop_active_in_txn` is still its own check alongside it | `queue.py:1656` `_stop_active_in_txn`, `:1662` `_stop_active_precheck`, called at `queue.py:1937`, `:2402/2410` and `:3531/3537` |
| 13 | `pause.json` additive STOP union | **REMOVED as a gate; dead definition remains** | `queue.py:1575` `def _pausejson_active` has no callers (`grep "_pausejson_active()"` finds nothing). `pause.py:126 _suppressed_path` is a state-file path, not a judgement |
| 14 | `risk_tier` enforcement path | **REMAINS, a claim.** It still decides cascades outside T1 | `risk_tier.py:30` `risk_tier_enforcement_enabled`, `:52` `classify_task`; used at `server.py:3931`, `pipeline.py:981`, `:1296` and `:1542/1543` |
| 15 | `coordinator_managed` cascade suppression | **REMOVED** | No `.get("coordinator_managed")` branch in src. It is provenance only (§7.2); fixture CX-4b shows an identical decision with and without it |
| 16 | G11 artefact gate | **REMAINS as T5** (target component) | `pipeline.py:200` |
| 17 | G_DT / #362 delivery guard | **REMAINS as T6.** The unknown-task part (`_guard_task_existence`) still exists separately instead of being absorbed into T3 ("no receipt ⇒ no dispatch") | `server.py:2632`, `queue.py:3346` |
| 9, 10 | G13 matcher / E4 `admission_decision` | Only the E4 lookup (T2) exists in agent_crew | no `def match_capabilities\|admission_decision\|reuse_preflight` in src (CXC-1) |

## 3. Count

The count includes the alfred-side rows, which are unchanged here and are not verified by this repo:

| | Count |
|---|---|
| Target components present (T1, T2, T3, T5, T6; T4 is alfred-side) | 5 in agent_crew |
| Extra judgements outside T1–T6 in agent_crew src | **2**: `runtime_stop` `_stop_active_*` (§11.2 #12) and the `risk_tier` path (#14) |
| Half-absorbed | 1: `_guard_task_existence` (#17 unknown-task part), which is still separate from T3 |
| Dead code, not a guard | `_pausejson_active` (#13): defined, but nothing calls it |

On the agent_crew side, then, there are **5 target components + 2 extras = 7**, with one partial on top. §11.2 wants 6 or fewer. Two things close that gap:

1. **#12:** make `_stop_active_in_txn` an input that T3 reads (the P6 row), not a verdict of its own. P6 says the `runtime_stop` row is *generalised*, so the separate check should be deleted, not kept next to the validator.
2. **#14:** move `risk_tier` classification into the snapshot matrix (O10, §11.3), where T1 reads it. The `pipeline.py` and `server.py` branches then stop deciding.

Deleting `queue.py:1575 _pausejson_active` and absorbing `_guard_task_existence` into T3 are clean-ups. Neither changes the count.

## 4. Commands to reproduce

```bash
cd src/agent_crew
grep -nE "_stop_active_in_txn\(|_stop_active_precheck\(" queue.py server.py
grep -nE "_pausejson_active\(\)" *.py
grep -nE "risk_tier_enforcement_enabled\(\)|classify_task\(" *.py
grep -nE "\.get\(.coordinator_managed.\)" *.py
grep -nE "duplicate.in.flight|DUPLICATE_IN_FLIGHT" *.py
grep -rnE "^def (match_capabilities|admission_decision|reuse_preflight)" .
grep -rn "policy_ack.json\|ssot_decision_ack.json" .
grep -nE "def _guard_task_existence|def defer_push_delivery|def artifact_gate_applies" *.py
```

A later step can turn the §3 count into a `CX-G` fixture. It is not one yet: the 4c lane is still moving #12 and #14.

## 5. Paths back to pending (4d-r2, Codex P1) — not in §11.2, but I2 call sites

`result`/`discovery` — code read at agent_crew `10bf4a1`/`5436e30` (s4d merged with `sev0/cea-lineage` `caf5644`). Each of these moves an `in_progress` task back to `pending`, i.e. back to claim/dispatch. None goes through the receipt lifecycle today.

| Path | Where | Receipt effect (measured, mode=test and shadow) |
|---|---|---|
| `TaskQueue.requeue` | `queue.py:3412` (literal `SET status = 'pending'` at `:3417`) | receipt stays **CLAIMED**, no lifecycle row |
| `TaskQueue.reset_stale_to_pending` | `queue.py:3469` (write at `:3487`) | same |
| `TaskQueue.defer_push_delivery` (G_DT backoff) | `queue.py:3373` (bound-parameter `"pending"` at `:3400`); caller `server.py:2675` | same |
| server startup `_requeue_orphans` | `server.py:2528` → `requeue` at `:2556` | same |
| other `requeue` callers (push/delivery failure paths) | `server.py:2723`, `:2730`, `:2915`, `:2925`, `:3594`, `:4336`, `:4515`, `:4549` | same |
| `crew recover --reset-stale` | `cli.py:1657` → `reset_stale_to_pending` at `:1898` | same |
| `retry.failed_task` | `server.py` `enqueue(retry_req, ingress="retry.failed_task")` | **not a requeue** — a new task and a new receipt through the one admission entry |

Consequence (`tests/unit/test_sev0_cea_i2_static_dynamic.py`):

- **Zero bypass holds:** under enforcing mode the claim gate refuses a receipt still in CLAIMED, so the requeued task is never re-dispatched on it (5 PASS).
- **Liveness does not:** the same task is left stranded in `pending` for good. `LIFECYCLE_GRAPH` has no CLAIMED→QUEUED edge. This is s4f item *requeue receipt lifecycle* (5 strict xfail).
- Under shadow the re-claim proceeds and adds a lifecycle row, so it is reported (5 PASS).

## 6. Transport findings from I1 (4d-r2) — code lane s4f

### Current classification: HTTP single-task recover (alfred#51 §15 item 14)

`POST /tasks/{task_id}/recover` is an ADR §8 **requeue ingress** because it can
move an in-progress task back to `pending`. It does not create a new task and
therefore is not a new `http.tasks` adapter. The route at `server.py:6541`
uses `TaskQueue.requeue` when forced and `TaskQueue.requeue_dispatcher_claim`
otherwise. Both call `requeue_through_gate` before their pending writes, within
the same transaction (`queue.py:4454`, `:4486`; receipt lifecycle at `:3074`).
`reset_stale_to_pending` likewise gates each selected row before its batch
pending write (`queue.py:4723`, `:4731`). The gate reuses a receipt in `HELD`
when an attempt remains, or records `SUPERSEDED` / `HELD` as ADR §8 requires;
the claim gate then enforces the receipt state. The forced HTTP transport and
dispatcher-claim writer are exercised by the I1 and I2 unit tests.

The historical measurements above describe the earlier 4d-r2 code, before the
§8 receipt lifecycle implementation.

- **empty-project admission:** `loop.enqueue_*` and `discussion.enqueue_panel_tasks` build a `TaskRequest` with no `project`, and `crew enqueue --db` without `--project` and `watch.run_cycle(project="")` do the same. The engine then raises `EngineError` (`$.project` must be non-empty) instead of writing a P2 BLOCK audit receipt, so the adapter crashes and nothing is persisted.
- **http refusal mapping:** `POST /tasks` catches only `TaskAlreadyExistsError`. A refused admission (`AdmissionRefused`) escapes as an unhandled 500, and the response carries no `receipt_id`.

## 7. I1 transports, all fourteen (4d-r3) — code lane s4f

`result` — agent_crew `c2de6db` on `sev0/cea-lineage-s4d` (after merging `sev0/cea-lineage` `b6be8d2`, s4e wiring). `TRANSPORTS == DOCUMENTED == adapters.BY_ID`; nothing is left undriven.

| Ingress | Real entry point driven | Equality with HTTP (3 authority states) |
|---|---|---|
| `http.tasks`, `cli.enqueue`, `cascade.review`, `cron.watch` | (4d-r2) | PASS |
| `cascade.fix` | `pipeline.auto_enqueue_fix` on a `request_changes` review | PASS |
| `cascade.test` | `pipeline.auto_enqueue_test` on an `approve` review | strict xfail — empty-project |
| `cascade.fallback` | `pipeline.auto_fallback_failed_task` on a 429-shaped failure | strict xfail — empty-project |
| `cron.triage` | `triage.enqueue_task` | strict xfail — empty-project |
| `loop.implement`, `loop.review`, `loop.test`, `cli.discuss` | `loop.enqueue_*`, `discussion.enqueue_panel_tasks` | strict xfail — empty-project |
| `retry.failed_task` | `POST /tasks/{id}/result` `status=failed` after dispatch → `/start` | strict xfail — empty-project |
| `watchdog.stale_review` | `POST /tasks/{id}/result` on a review whose PR head moved (`review_publication_decision` stubbed to requeue) | strict xfail — empty-project |

`test_every_transport_reaches_admission_as_its_own_ingress` (not xfailed, 14 PASS) pins that each driver reaches the admission entry under its own ingress id. For the nine project-less adapters, it also pins that admission raised on `project=""`. A strict xfail therefore cannot pass for the wrong reason, such as a broken driver.

- **empty-project admission (extended):** the same defect now also covers `pipeline.auto_enqueue_test`, `pipeline.auto_fallback_failed_task`, `triage.enqueue_task`, server `_auto_retry_failed_task` and server `_requeue_review_at_head`. Each builds its `TaskRequest` without `project`. In the pipeline/server paths the resulting exception is swallowed by the cascade's own `try`, so the successor is silently never created and no receipt exists.
- **MCP `submit_result` (P2 RESULT, not an ingress):** `get_next_task` (nonce) → HTTP `/start` → MCP `submit_result(executor_binding)` persists the same `(status, decision, reason, intent_hash, state, nonce spent)` as the all-HTTP flow. Without `/start`, both are refused `NONCE_NOT_STARTED`, the row stays `in_progress` and the nonce is not spent (2 PASS, `test_sev0_cea_i1_mcp_result.py`).
- **Harness finding (fixed in the test lane):** since s4e, `create_app` passes `install_from_env` providers explicitly. The r2 `inject_cea` used `setdefault`, so every HTTP drive after the merge ran `mode=shadow` against the live alfred governance inputs and set the process-global runtime authority. `inject_cea` now forces mode + fixture providers and stubs `install_from_env`.

## 8. FINAL state at the end of the CEA lineage (s4g)

`result` — greps in §4 re-run on the merged lineage head (the s4d lane folded in
at `4a6ca04`). This is the state the lineage ends in, not a plan.

### Guard count vs ADR §11.2

| | Count | Δ since §3 |
|---|---|---|
| Target components present in agent_crew (T1, T2, T3, T5, T6) | 5 | — |
| Extra judgements outside T1–T6 | **2** — `runtime_stop` `_stop_active_*` (#12), `risk_tier` path (#14) | — |
| Half-absorbed | 1 — `_guard_task_existence` (#17), still separate from T3 | — |
| Dead definition, not a guard | `_pausejson_active` (#13), still zero callers | — |

**5 + 2 = 7. ADR §11.2 wants ≤ 6, so the gap is NOT closed by this lineage.**
The two named reductions (#12 into the P6 row T3 already reads, #14 into the O10
snapshot matrix) are unstarted. Reporting 7 rather than 6 is the honest count;
nothing in s4d–s4g removed a guard.

Line numbers moved with the merge. Re-measured on the merged head:

- #12 `runtime_stop`: **REMAINS**, 7 sites, all in `queue.py` — defs at `:1657`
  `_stop_active_in_txn` / `:1663` `_stop_active_precheck`, called at `:1964`,
  `:2429`, `:2437`, `:3558`, `:3564`.
- #13 `_pausejson_active`: def at `queue.py:1576`, **still no callers**.
- #14 `risk_tier`: **REMAINS**, now `pipeline.py:981`, `:1296`, `:1542/1543` and
  `risk_tier.py:30/52` (+ internal `:144/162/168/176`). The `server.py` call site
  quoted in §2 is **gone** — `grep` on `server.py` finds none.
- #11 duplicate-in-flight, #15 `coordinator_managed`: still **REMOVED** (no hits).
- T5 `artifact_gate_applies` `pipeline.py:200`; T6 `defer_push_delivery`
  `queue.py:3373`, `_guard_task_existence` `server.py:2669`.
- T3 gates: `queue.py:1948` (enqueue), `:2162` (claim), `:2579` (result).

### §6 transport findings — final disposition

- **empty-project admission:** still **DEFERRED**. The engine raises
  `EngineError` (`$.project` non-empty) instead of writing a P2 BLOCK receipt.
  Verified by running the markers with `--runxfail` on the final head: the
  failure is that `EngineError` at `cea/engine.py:1170`, i.e. the marker's stated
  reason, not a stale one. 41 strict xfail.
- **http refusal mapping:** **DONE** in `fc857aa` (s4f). An app-level
  `AdmissionRefused` handler maps the machine reason code to 401/403/409/423 and
  carries the `receipt_id`; the marker came off in that same commit, so it is no
  longer in the xfail list.
- **Harness finding:** the `inject_cea` fix in `c2de6db` covered the shared
  helpers but **not** `test_sev0_cea_s4b_result_requires_start.py`, which kept
  its own `enforcing_queues` fixture on `kw.setdefault` and did not stub
  `install_from_env`. Merging s4d-r3 turned the whole CEA suite red on
  `test_http_result_before_start_is_refused` (403 `DECISION_BLOCK` from the LIVE
  unkeyed snapshot at enqueue — P7 — not the rule under test). Fixed in s4g by
  applying the same two changes there. No other CEA file has the pattern:
  of the files that call `create_app`, the rest use the hermetic helpers or
  stub `install_from_env` themselves.

## 9. s4m — the two extras are gone, and the count is executable

`result` — agent_crew `sev0/cea-lineage` at s4m. Codex acceptance finding [2] at
`c18e092` (P1) was that §8 honestly reported **7** against ADR §11.2's `<= 6`.
Both named extras are now relays to a single implementation.

    GUARD_COUNT = 5

That literal is not decoration: `tests/unit/test_sev0_cea_guard_count.py`
parses it and fails if this document and the code disagree. A hand count is a
claim about a moment; the probes in that file re-derive it on every run, which
is what makes the acceptance checkable rather than re-argued.

| | Count | Δ since §8 |
|---|---|---|
| Target components present in agent_crew (T1, T2, T3, T5, T6) | 5 | — |
| Extra judgements outside T1–T6 | **0** | −2 |
| **Total** | **5** | −2 |

T4 (the snapshot producer's ack ledger) stays alfred-side and is not counted
here — counting a component this repo does not implement would inflate the very
number the ADR bounds (CXC-1 still finds no ack ledger in src).

### #12 `runtime_stop` — the queue relays, P6 decides

`queue.py` compared the row against `"ACTIVE"` itself. That comparison is one
implementation of the subject matter T3 already owns, so it counted as a second
guard.

- `cea/validator.py` now exposes `runtime_state_verdict(state, point)` — the
  **one** implementation of the P6 enforcement matrix. `validate()` calls it;
  it is no longer inline there either.
- `cea/callsites.py` `gate_runtime_state()` is the relay the queue uses.
- `queue.TaskQueue._stop_active_in_txn` / `_stop_active_precheck` now read the
  row (only they hold the write lock) and return **the gate's** answer. The
  enqueue call site names `ValidationPoint.ENQUEUE`; the two claim sites use
  the CLAIM default.

Behaviour is unchanged, and pinned as such:
`test_runtime_state_verdicts_all_come_from_one_matrix` asserts the relay
reproduces `state != "ACTIVE"` for ENQUEUE and CLAIM across all four states plus
an unknown one (unknown ⇒ STOPPED ⇒ refused, P7).

⛔The gate is deliberately **not** conditioned on `enforcing()`. Shadow/enforce
  governs whether a *receipt* verdict stops work; the #314 operator STOP is not
  a receipt verdict, and coupling them would mean turning CEA off also turned
  the fleet pause off.

**Two `!= "ACTIVE"` comparisons remain in `queue.py` and are not this row:**
`:3121` (`_suppressed` — successor suppression at RESULT) and `:3483` (the §8
requeue path). Neither is a P2 admission gate, and neither was in the finding.
Folding them in is not a relocation — the P6 matrix says RESULT under DRAINING
is `ok`, where `!= "ACTIVE"` suppresses — so it would change which successors
are created. That is a behaviour change and needs its own step. The guard-count
probe for #12 is scoped to the `_stop_active_*` family for exactly this reason,
and says so in its own comment rather than implying the other two do not exist.

### #14 `risk_tier` — decided at admission, consumed by the cascade

`pipeline.py` re-asked `risk_tier_enforcement_enabled()` and `classify_task()`
at every cascade step. Now `cea/cascade_contract.py` `decide()` runs **once**,
on the admission path in `TaskQueue.enqueue_with_receipt` — where T1 already is
— and stores its answer on the row as `context["cea_cascade"]`.

- `pipeline.py` imports nothing from `agent_crew.risk_tier`. `grep -nE
  "classify_task|risk_tier_enforcement_enabled|cascade_metadata|
  effective_fix_round_cap|shadow_decision" pipeline.py` finds nothing.
- The cascade reads `_cascade.stored(task)` and consumes `needs_reviewer`,
  `needs_tester`, `review_mode`, `test_scope`, `human_gate_required` and
  `fix_round_cap(ceiling)`. Checking whether a Tier 3 gate has been *approved*
  stays in the cascade: that is a read of durable row state, not a judgement.
- A row with no stored contract (admitted before this step) gets the floor —
  review and test everything, gate nothing. Under P7's asymmetry an unknown
  contract buys *more* independent scrutiny, never less.
- `server.py`'s dispatch path was already a pure consumer (its `test_scope`
  comment). This makes the cascade match it.

**What this step does not do:** raise the contract to the receipt's J7 floor.
`engine._j7_contract` names a required reviewer/tester for essentially every
`implement` work class (`REVIEW_FLOOR`), so honouring it as a floor would make
Tier 0's implement-only cascade unreachable — a change to *which tasks get
reviewed*, not to where the decision lives. It was measured: applying it turned
`test_low_tiers_reduce_automatic_cascade_and_fix_budget_when_enforced` red. The
J7 answer is therefore **recorded** on the contract (`j7_reviewer`,
`j7_tester`) so the disagreement is visible and countable, and nothing acts on
it yet. That is the next step, not this one.

### Evidence

- `tests/unit/test_sev0_cea_guard_count.py` — 13 PASS. Enumerates T1–T6 (each
  asserted present), probes for extras, asserts `<= 6`, and cross-checks
  `GUARD_COUNT` above.
- CEA suites green: see the s4m commit message for counts.
- **Pre-existing reds, not caused by s4m:**
  `test_issue_278_test_economics.py::test_stale_risk_tier_targeted_scope_*` (2)
  fail identically at `191f258` — verified by running them against a clean
  `git archive HEAD` tree. They are about `server.py`'s dispatch path, which
  s4m does not touch, and they encode the pre-consumer expectation that
  survived the step that made `server.py` a consumer. They need their own
  decision about expected behaviour.
