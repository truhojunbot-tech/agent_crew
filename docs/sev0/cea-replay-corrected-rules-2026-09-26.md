# CEA shadow replay under #410 and the staged ENFORCE allowlist

result — Read-only replay of the 2026-09-26 [precision audit](cea-shadow-block-precision-2026-09-26.md). The script read only the nine SQLite backups in `/tmp/cea-shadow-audit-20260926-r2/`; it did not read live databases or change runtime configuration.

## Headline

- **180** distinct `seq=0` decisions, including **131** non-ALLOW decisions, matched the audit. The audit's **70** labeled samples matched by project and receipt prefix: 15 supported, 16 false blocks, 39 unknown.
- #410 releases **7 of 13** `ALREADY_COMPLETED` decisions because the referenced task is known to have failed or timed out. The other **6** retain `ALREADY_COMPLETED` because the referenced task completed. No referenced task status in these 13 was missing or unknown. The seven released decisions need another engine evaluation; their final codes cannot be inferred from receipt data.
- With `AGENT_CREW_CEA_ENFORCE_CODES=RUNTIME_STATE_FORBIDS`, **4** recorded decisions would STOP work. All four are audit-labeled supported. **0 of 16** labeled false blocks would STOP on their recorded code. One of those 16 is among the seven released decisions, so the stronger claim that *no false block could stop after a full re-evaluation* is unproven.
- **1** supported hard block would become advisory: `halla` receipt `194f8200-45f6-4af5-8c91-de40da46d0d9` (`DUPLICATE_INTENT`). Another **10** supported `REVIEW` escalations would proceed with an advisory flag; their audit label supports escalation, not a hard execution block.
- In **alfred's 22 decisions**, the recorded outcomes are 8 `ALLOW:OK`, 5 `HUMAN_GATE:OWNER_CONFLICT`, and 9 `REVIEW:IDENTITY_UNVERIFIED_REVIEW_REQUIRED`. #410 changes none. Under the allowlist, all 22 proceed; 14 non-ALLOW outcomes remain advisory. There is no alfred `RUNTIME_STATE_FORBIDS` receipt in this snapshot, so it cannot measure alfred STOP precision.

## Decision and code counts

The replay applies #410 to `ALREADY_COMPLETED` using the prior receipt's task status in the copied database. `RE_EVALUATE:UNKNOWN` means the old refusal is removed, then the engine must continue evaluating inputs that the snapshot does not preserve at the necessary historical point. It is a replay result bucket, **not** a CEA reason code.

| Decision / code | Before | After #410 | STOP on recorded code with allowlist |
|---|---:|---:|---:|
| `ALLOW:OK` | 49 | 49 | 0 |
| `BLOCK:ALREADY_COMPLETED` | 13 | 6 | 0 |
| `BLOCK:BUDGET_EXHAUSTED` | 18 | 18 | 0 |
| `BLOCK:BUDGET_UNVERIFIED` | 10 | 10 | 0 |
| `BLOCK:DUPLICATE_INTENT` | 1 | 1 | 0 |
| `BLOCK:HUMAN_GATE_DENIED` | 34 | 34 | 0 |
| `BLOCK:RUNTIME_STATE_FORBIDS` | 4 | 4 | 4 |
| `HUMAN_GATE:HUMAN_GATE_PENDING` | 5 | 5 | 0 |
| `HUMAN_GATE:OWNER_CONFLICT` | 10 | 10 | 0 |
| `REVIEW:IDENTITY_UNVERIFIED_REVIEW_REQUIRED` | 36 | 36 | 0 |
| `RE_EVALUATE:UNKNOWN` | 0 | 7 | Unknown |
| **Total** | **180** | **180** | **4 known** |

The four known STOP receipts are `agent_crew/190c4cc3-f710-4568-be64-c75e9c073546`, `agent_crew/3095ec80-79a1-45ca-9859-1bed95e1d862`, `agent_crew/7da6d85b-cfa6-4c92-a10b-dfb973b0f267`, and `agent_crew/cbd189a9-2de1-4d3d-ba05-8f0f8dbdda42`. All are `RUNTIME_STATE_FORBIDS`.

## Changed `ALREADY_COMPLETED` receipts

| Project / receipt | Prior task status in backup | Audit label |
|---|---|---|
| `agent_crew/02243059-89a7-42cf-8c84-4a379b5f3eaf` | timed_out | UNKNOWN |
| `agent_crew/3708e980-ff78-4db1-9bd8-8b0b9d81fc1e` | timed_out | UNKNOWN |
| `agent_crew/7567a365-fec7-418b-8709-9554519b4416` | timed_out | UNKNOWN |
| `agent_crew/8354a561-5a7c-44bf-a0c4-0ba4d1115449` | timed_out | UNKNOWN |
| `agent_crew/dede5db9-4561-425f-ae74-a9379bd4b190` | timed_out | FALSE_BLOCK |
| `agent_crew/ea3ec9f9-5161-497a-bfa3-f5c5d35bde41` | failed | unsampled |
| `agent_crew/f11b7f7c-c6c2-4b0b-bc6e-66f060b9c871` | failed | unsampled |

The five timed-out rows all reference the same earlier receipt. This is a per-decision counterfactual against the stored backup, not a sequential reconstruction of a live queue. The other six `ALREADY_COMPLETED` decisions reference tasks now marked `completed` and remain refused by #410.

## Supported decisions that would proceed

The single supported **hard-block miss** is `halla/194f8200-45f6-4af5-8c91-de40da46d0d9` (`DUPLICATE_INTENT`). The following ten supported `REVIEW` decisions become advisory, retaining their original review outcome and reason code:

`agent_crew/0336794a-15cd-4f26-bbda-d512f91c4f62`, `agent_crew/4e1aaff2-a50e-4ef7-aa7b-1238060974ea`, `alfred/449c4638-2e51-4713-9d22-7841b5d8c93a`, `alfred/e6b357e9-45de-402c-a746-622659e3bac7`, `alpha_engine/61f2c8d8-af1f-44b6-b8f9-e17435af3bd5`, `alpha_engine/d93a3ca2-6e82-4e5e-add7-48e1e8279f4f`, `halla/01a950cf-a699-49a3-963f-7439e82b666d`, `halla/6564d358-bf80-49d3-b6bc-94c58f9b0831`, `quota-ops/2294ecdb-3490-4184-b774-00e70ddace3c`, `quota-ops/fb6c0abf-daac-4a66-b494-78d231a7753a`.

## Method and limits

Run `PYTHONPATH=src python3 scripts/replay_cea_corrected_rules.py --snapshot-dir /tmp/cea-shadow-audit-20260926-r2 --audit docs/sev0/cea-shadow-block-precision-2026-09-26.md`. The script opens each backup with SQLite `mode=ro&immutable=1`, reads `seq=0` receipts, parses the lineage receipt named in each `ALREADY_COMPLETED` reason, then calls the current `cea.store.task_status` and checks `cea.engine.RELEASABLE_CONSUMED_TASK_STATUSES`. It applies `cea.callsites.enforcing` with `EngineConfig(mode="enforce", enforce_codes={"RUNTIME_STATE_FORBIDS"})`. Receipt prefixes and labels come from the audit document.

The task statuses are those at backup time (about 04:39 UTC), not necessarily at the original admission time. The backup does not preserve historical policy, budget, capability, gate, and runtime provider answers for a newly admitted intent after a #410 release. Its old `ALREADY_COMPLETED` receipt binding contains placeholders such as `not-consulted`; using those as a fresh input would fabricate a verdict. Thus seven final codes and any STOPs they might yield are unknown. The simulation applies alfred's planned allowlist to all nine copies for sample comparison; only the alfred subset describes that project's staged rollout. It does not predict future decisions or establish a population precision rate from the purposive 70-sample audit.
