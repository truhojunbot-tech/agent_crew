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
