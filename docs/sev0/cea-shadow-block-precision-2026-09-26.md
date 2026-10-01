# CEA shadow decision precision audit — 2026-09-26

result — Read-only audit of authorization receipts in nine project databases. This is a purposive sample, not an estimate of population precision. No enforcement flag or live database was changed.

## Method and limits

- Snapshot: SQLite online backups of every `~/.agent_crew/*/tasks.db` to `/tmp/cea-shadow-audit-20260926-r2/`; source connections used `mode=ro`. Analysis queried only these copies. The snapshot was taken at about 04:39 UTC on 2026-09-26. Its 992 `authorization_receipts` lifecycle rows represent **180 distinct receipt decisions** (`seq=0`); 49 ALLOW and 131 BLOCK/REVIEW/HUMAN_GATE. The owner-provided 863-row breakdown describes an earlier snapshot and appears to count lifecycle rows, so it is not the denominator below.
- Sampling: up to ten `seq=0` receipts per decision/reason code. Mostly earliest cases, with specified later same-provider or cross-project cases to test known failure modes. REVIEW was balanced at two cases per project. These choices are not random; the ratios below are diagnostic only.
- Join: match `tasks.receipt_id = authorization_receipts.receipt_id`. A task with the same `task_id` but a different receipt is **not** counted as work from the sampled receipt. `task_attribution` supplies observed token totals (uncached input + cache write + cache read + output); `≥` means at least one field was NULL, and `unreported` is not zero. Token counts do not establish money cost or whether work was useful.
- Labels: `CORRECT_BLOCK` means independently supported refusal, or a correct REVIEW escalation; `FALSE_BLOCK` means the shadow decision would have suppressed distinct productive work or same-provider work that demonstrably ran; `UNKNOWN` means ownership, quota, or work equivalence could not be established from the copied databases. For REVIEW, the label evaluates escalation, **not** a hard execution block.
- Precision = `CORRECT_BLOCK / (CORRECT_BLOCK + FALSE_BLOCK)` among resolved sampled cases. `UNKNOWN` is excluded and shown separately. This is not a statistical confidence interval. Task completion alone never proves owner authorization or provider quota.

## Per-code sample precision

| Decision / reason | Population receipts | Sample | Correct | False | Unknown | Resolved precision |
|---|---:|---:|---:|---:|---:|---:|
| `BLOCK:ALREADY_COMPLETED` | 13 | 10 | 0 | 5 | 5 | 0/5 (0%) |
| `BLOCK:BUDGET_EXHAUSTED` | 18 | 10 | 0 | 2 | 8 | 0/2 (0%) |
| `BLOCK:BUDGET_UNVERIFIED` | 10 | 10 | 0 | 9 | 1 | 0/9 (0%) |
| `BLOCK:DUPLICATE_INTENT` | 1 | 1 | 1 | 0 | 0 | 1/1 (100%) |
| `BLOCK:HUMAN_GATE_DENIED` | 34 | 10 | 0 | 0 | 10 | N/A (0 resolved) |
| `BLOCK:RUNTIME_STATE_FORBIDS` | 4 | 4 | 4 | 0 | 0 | 4/4 (100%) |
| `HUMAN_GATE:HUMAN_GATE_PENDING` | 5 | 5 | 0 | 0 | 5 | N/A (0 resolved) |
| `HUMAN_GATE:OWNER_CONFLICT` | 10 | 10 | 0 | 0 | 10 | N/A (0 resolved) |
| `REVIEW:IDENTITY_UNVERIFIED_REVIEW_REQUIRED` | 36 | 10 | 10 | 0 | 0 | 10/10 (100%) |

## Recommendation

1. **Enforce `RUNTIME_STATE_FORBIDS` first.** All four available receipts independently match the most recent `runtime_state_events` state `STOPPED` at issuance, and none owns a task row. Keep the existing atomic STOP check as the final authority.
2. **Keep `REVIEW:IDENTITY_UNVERIFIED_REVIEW_REQUIRED` as an escalation, not a hard block.** All ten sampled receipts record `UNVERIFIED` caller identity and the shared-UID downgrade. The routing decision is supported; a hard refusal has not been audited.
3. **Consider `DUPLICATE_INTENT` only for exact live-intent matches after more samples.** The sole receipt correctly found another live same-intent review; the shadow task then reached `needs_human` with a stale SHA and consumed observed tokens. One sample is too little to enable broadly.
4. **Do not enforce `ALREADY_COMPLETED` yet.** Five of ten sampled cases were false blocks: four clear new-head or new-fix work plus one newer-head review in alpha_engine. Intent identity needs head/round or a verified superseding decision before a completed receipt vetoes another task.
5. **Do not enforce either budget code yet.** Nine of ten `BUDGET_UNVERIFIED` samples were productive Gemini tests despite an April observation timestamp; actual Gemini token fields were unreported, so the quota itself remains unknown. In `BUDGET_EXHAUSTED`, most samples budgeted Codex but actually dispatched Claude, while two Gemini tests ran under a Gemini-exhausted decision. The budget decision must use fresh evidence for the actual selected provider and distinguish fallback from refusal.
6. **Do not turn `HUMAN_GATE_DENIED`, `HUMAN_GATE_PENDING`, or `OWNER_CONFLICT` into a blanket automatic block from this dataset.** The receipts state gate outcomes, but these copies do not carry independently authenticated owner grant/denial records for the sampled tasks. Several shadow tasks ran and consumed measured tokens, so the gate feed and provenance need reconciliation before precision can be established. Preserve a human gate for live safety decisions.

## Sample evidence

Receipt IDs are shown as unique eight-character prefixes within this snapshot; the full receipt can be found by querying `authorization_receipts.receipt_id LIKE 'prefix%'` in the named project copy. A result/status is reported only for an exact receipt-to-task join.

### `BLOCK:ALREADY_COMPLETED` (10 sampled of 13)

| Project / receipt | Task | Exact outcome | Label | Evidence |
|---|---|---|---|---|
| `agent_crew` / `02243059` | `sev0-tokenomics-canary-one-r1c-loop-semantics` | cancelled | **UNKNOWN** | prior sev0-tokenomics-canary-one-r1b (timed_out), head unpinned; current cancelled; tokens ≥6,075,677 |
| `agent_crew` / `dede5db9` | `sev0-tokenomics-canary-one-r2-atomic-suppression` | completed | **FALSE_BLOCK** | prior sev0-tokenomics-canary-one-r1c-loop-semantics (cancelled), head unpinned; current completed; tokens 3,073,460 |
| `agent_crew` / `7567a365` | `sev0-tokenomics-feedback-loop-x` | failed | **UNKNOWN** | prior sev0-tokenomics-canary-one-r2-atomic-suppression (completed), head unpinned; current failed; tokens 4,435,962 |
| `agent_crew` / `3708e980` | `fix-review-sev0-tokenomics-feedback-loop-x-r1-r1` | cancelled | **UNKNOWN** | prior sev0-tokenomics-feedback-loop-x (failed), head unpinned; current cancelled; tokens unreported |
| `agent_crew` / `8354a561` | `sev0-tokenomics-feedback-loop-pass2-x` | failed | **UNKNOWN** | prior fix-review-sev0-tokenomics-feedback-loop-x-r1-r1 (cancelled), head unpinned; current failed; tokens 2,262,448 |
| `agent_crew` / `16e20586` | `review-fix-review-p01-336-cancel-terminates-worker-r2-r0-r1-r1` | completed / approve | **FALSE_BLOCK** | prior review-p01-336-cancel-terminates-worker-r2-r0 (completed), head 4c7e004→63f8001; current completed / approve; tokens 5,264,237 |
| `agent_crew` / `336a7604` | `review-fix-review-p01-362-exclusive-port-r2-r0-r1-r1` | completed / approve | **FALSE_BLOCK** | prior review-p01-362-exclusive-port-r2-r0 (completed), head e9f8a00→6282f1d; current completed / approve; tokens 3,950,957 |
| `agent_crew` / `63c9b062` | `review-fix-review-p01-348-run-branch-persist-r0-r1-r1` | completed / approve | **FALSE_BLOCK** | prior review-p01-348-run-branch-persist-r0 (completed), head 2cd5b56→14e476c; current completed / approve; tokens 3,642,587 |
| `agent_crew` / `f07cb4c0` | `p01-review-pr377-codex` | completed / request_changes | **UNKNOWN** | prior p01-review-pr377 (completed), same head e1ad4d5; current completed / request_changes; tokens 15,477,030 |
| `alpha_engine` / `a16e32c3` | `review-5691-r2` | completed / approve | **FALSE_BLOCK** | prior review-5691-r1 (completed), head 04f8aab→1f12998; current completed / approve; tokens 5,926,396 |

### `BLOCK:BUDGET_EXHAUSTED` (10 sampled of 18)

| Project / receipt | Task | Exact outcome | Label | Evidence |
|---|---|---|---|---|
| `agent_crew` / `bc459544` | `review-sev0-tokenomics-canary-one-x-r0` | completed / request_changes | **UNKNOWN** | budget EXHAUSTED for codex (no observation time); dispatch claude, completed; tokens 3,364,372 |
| `agent_crew` / `2e1a18ce` | `review-sev0-tokenomics-canary-one-r1-verdict-ordering-r0` | completed / request_changes | **UNKNOWN** | budget EXHAUSTED for codex (no observation time); dispatch claude, completed; tokens 3,003,825 |
| `agent_crew` / `5ef98e41` | `review-fix-review-sev0-tokenomics-canary-one-x-r0-r1-r1` | completed / request_changes | **UNKNOWN** | budget EXHAUSTED for codex (no observation time); dispatch claude, completed; tokens 3,798,718 |
| `agent_crew` / `ce84f6e3` | `review-sev0-tokenomics-canary-one-r2-atomic-suppression-r0` | completed / request_changes | **UNKNOWN** | budget EXHAUSTED for codex (no observation time); dispatch claude, completed; tokens 3,501,408 |
| `agent_crew` / `da7e2ed1` | `review-fix-review-sev0-tokenomics-canary-one-r2-atomic-suppression-r0-r1-r1` | completed / request_changes | **UNKNOWN** | budget EXHAUSTED for codex (no observation time); dispatch claude, completed; tokens 2,338,949 |
| `agent_crew` / `db15ea15` | `review-fix-review-fix-review-sev0-tokenomics-canary-one-r2-atomic-suppression-r0-r1-r1-r2-r2` | completed / approve | **UNKNOWN** | budget EXHAUSTED for codex (no observation time); dispatch claude, completed; tokens 2,354,174 |
| `agent_crew` / `fe4e1861` | `test-review-fix-review-fix-review-sev0-tokenomics-canary-one-r2-atomic-suppression-r0-r1-r1-r2-r2` | completed | **FALSE_BLOCK** | budget EXHAUSTED for gemini (2026-04-10T06:09:47Z); dispatch gemini, completed; tokens unreported |
| `agent_crew` / `eae793ce` | `review-sev0-p0-2-g12-cancel-invariants-r1-r0` | completed / request_changes | **UNKNOWN** | budget EXHAUSTED for codex (no observation time); dispatch claude, completed; tokens 3,245,788 |
| `agent_crew` / `e03bc263` | `review-sev0-p0-1-step2-r1-tests-r0` | cancelled | **UNKNOWN** | budget EXHAUSTED for codex (no observation time); dispatch none, cancelled; tokens unreported |
| `agent_crew` / `eaf1a913` | `test-review-sev0-p0-2-broad-regression-reconcile-r0` | completed | **FALSE_BLOCK** | budget EXHAUSTED for gemini (2026-04-10T06:09:47Z); dispatch gemini, completed; tokens unreported |

### `BLOCK:BUDGET_UNVERIFIED` (10 sampled of 10)

| Project / receipt | Task | Exact outcome | Label | Evidence |
|---|---|---|---|---|
| `agent_crew` / `df55089a` | `test-review-p01-340-per-task-timeout-r0` | completed | **FALSE_BLOCK** | budget UNVERIFIED for gemini (2026-04-10T06:09:47Z); dispatch gemini, completed; tokens unreported |
| `agent_crew` / `d5f94afd` | `test-review-p01-374-rebase-gate-r0` | completed | **FALSE_BLOCK** | budget UNVERIFIED for gemini (2026-04-10T06:09:47Z); dispatch gemini, completed; tokens unreported |
| `alpha_engine` / `a40a2e50` | `test-review-5692-r1` | completed | **FALSE_BLOCK** | budget UNVERIFIED for gemini (2026-04-10T06:09:47Z); dispatch gemini, completed; tokens unreported |
| `alpha_engine` / `0934f67c` | `test-review-5691-r2` | completed | **FALSE_BLOCK** | budget UNVERIFIED for gemini (2026-04-10T06:09:47Z); dispatch gemini, completed; tokens unreported |
| `alpha_engine` / `534cdcf6` | `test-review-impl-a2-k1-rollback-r0` | completed | **FALSE_BLOCK** | budget UNVERIFIED for gemini (2026-04-10T06:09:47Z); dispatch gemini, completed; tokens unreported |
| `halla` / `893e2daf` | `test-review-25ab0660` | completed | **FALSE_BLOCK** | budget UNVERIFIED for gemini (2026-04-10T06:09:47Z); dispatch gemini, completed; tokens unreported |
| `halla` / `67198d66` | `test-91c39317` | completed | **FALSE_BLOCK** | budget UNVERIFIED for gemini (2026-04-10T06:09:47Z); dispatch gemini, completed; tokens unreported |
| `halla` / `49f374f6` | `test-review-fix-review-impl-fd0822fc-r0-r1-r1` | completed | **FALSE_BLOCK** | budget UNVERIFIED for gemini (2026-04-10T06:09:47Z); dispatch gemini, completed; tokens unreported |
| `halla` / `f5fb5030` | `test-review-23-1796ecdd566d` | needs_human | **UNKNOWN** | budget UNVERIFIED for gemini (2026-04-10T06:09:47Z); dispatch gemini, needs_human; tokens unreported |
| `halla` / `7c3845cb` | `test-review-impl-01916ce0-r0` | completed | **FALSE_BLOCK** | budget UNVERIFIED for gemini (2026-04-10T06:09:47Z); dispatch gemini, completed; tokens unreported |

### `BLOCK:DUPLICATE_INTENT` (1 sampled of 1)

| Project / receipt | Task | Exact outcome | Label | Evidence |
|---|---|---|---|---|
| `halla` / `194f8200` | `review-impl-cec8245e-r0` | needs_human / request_changes | **CORRECT_BLOCK** | prior same-intent task review-impl-fd0822fc-r0 was live at issue time, later request_changes; current needs_human, stale SHA, tokens 1,464,904 |

### `BLOCK:HUMAN_GATE_DENIED` (10 sampled of 34)

| Project / receipt | Task | Exact outcome | Label | Evidence |
|---|---|---|---|---|
| `agent_crew` / `576d2c1c` | `sev0-tokenomics-matched-evidence-x` | failed | **UNKNOWN** | gate DENIED, no claim in task; dispatch claude, failed; tokens 3,486,363 |
| `agent_crew` / `9bb6acd2` | `sev0-tokenomics-canary-one-x` | completed | **UNKNOWN** | gate DENIED, no claim in task; dispatch claude, completed; tokens 6,090,016 |
| `agent_crew` / `c4165200` | `sev0-tokenomics-canary-one-r1-verdict-ordering` | completed | **UNKNOWN** | gate DENIED, no claim in task; dispatch claude, completed; tokens 1,587,985 |
| `agent_crew` / `6ec403bb` | `fix-review-sev0-tokenomics-canary-one-x-r0-r1` | completed | **UNKNOWN** | gate DENIED, no claim in task; dispatch codex, completed; tokens 103,292,757 |
| `agent_crew` / `aed54a21` | `fix-review-sev0-tokenomics-canary-one-r1-verdict-ordering-r0-r1` | cancelled | **UNKNOWN** | gate DENIED, no claim in task; dispatch none, cancelled; tokens unreported |
| `agent_crew` / `6b65672c` | `fix-review-fix-review-sev0-tokenomics-canary-one-x-r0-r1-r1-r2` | cancelled | **UNKNOWN** | gate DENIED, no claim in task; dispatch none, cancelled; tokens unreported |
| `agent_crew` / `19de78e2` | `fix-review-sev0-tokenomics-canary-one-r2-atomic-suppression-r0-r1` | completed | **UNKNOWN** | gate DENIED, no claim in task; dispatch codex, completed; tokens 4,784,828 |
| `agent_crew` / `bf69705b` | `fix-review-fix-review-sev0-tokenomics-canary-one-r2-atomic-suppression-r0-r1-r1-r2` | completed | **UNKNOWN** | gate DENIED, no claim in task; dispatch codex, completed; tokens 5,849,518 |
| `agent_crew` / `14bcfb14` | `review-sev0-tokenomics-feedback-loop-x-r1` | completed / request_changes | **UNKNOWN** | gate DENIED, no claim in task; dispatch codex, completed; tokens 738,711 |
| `alpha_engine` / `c1397432` | `impl-a2-k1-rollback` | completed | **UNKNOWN** | gate DENIED, no claim in task; dispatch codex, completed; tokens 152,144,282 |

### `BLOCK:RUNTIME_STATE_FORBIDS` (4 sampled of 4)

| Project / receipt | Task | Exact outcome | Label | Evidence |
|---|---|---|---|---|
| `agent_crew` / `190c4cc3` | `sev0-j6-cea-budget-account` | no task from this receipt | **CORRECT_BLOCK** | runtime event epoch 34 = STOPPED; no exact task row |
| `agent_crew` / `3095ec80` | `sev0-item1-dup-probe-sqlite-shadow-report` | no task from this receipt | **CORRECT_BLOCK** | runtime event epoch 66 = STOPPED; no exact task row |
| `agent_crew` / `cbd189a9` | `sev0-issue364-codex-cap` | no task from this receipt | **CORRECT_BLOCK** | runtime event epoch 66 = STOPPED; no exact task row |
| `agent_crew` / `7da6d85b` | `sev0-issue364-codex-cap` | no task from this receipt | **CORRECT_BLOCK** | runtime event epoch 66 = STOPPED; no exact task row |

### `HUMAN_GATE:HUMAN_GATE_PENDING` (5 sampled of 5)

| Project / receipt | Task | Exact outcome | Label | Evidence |
|---|---|---|---|---|
| `agent_crew` / `21122a3e` | `sev0-tokenomics-canary-one-r1b` | timed_out | **UNKNOWN** | gate PENDING, no claim in task; dispatch claude, timed_out; tokens 6,971,714 |
| `agent_crew` / `4c779d56` | `p01-340-per-task-timeout` | completed | **UNKNOWN** | gate PENDING, no claim in task; dispatch codex, completed; tokens 1,680,485 |
| `agent_crew` / `4e42ce5d` | `p01-374-rebase-gate` | completed | **UNKNOWN** | gate PENDING, no claim in task; dispatch codex, completed; tokens 1,883,230 |
| `agent_crew` / `f856a293` | `p01-336-cancel-terminates-worker` | failed | **UNKNOWN** | gate PENDING, no claim in task; dispatch codex, failed; tokens unreported |
| `agent_crew` / `55c8d97c` | `p01-336-cancel-terminates-worker-r2` | completed | **UNKNOWN** | gate PENDING, no claim in task; dispatch codex, completed; tokens 2,825,293 |

### `HUMAN_GATE:OWNER_CONFLICT` (10 sampled of 10)

| Project / receipt | Task | Exact outcome | Label | Evidence |
|---|---|---|---|---|
| `agent_crew` / `5b817578` | `sev0-issue364-codex-cap` | failed | **UNKNOWN** | gate NOT_REQUIRED, claim present; dispatch codex, failed; tokens unreported |
| `agent_crew` / `2241d8f0` | `sev0-issue364-codex-cap-r2` | completed | **UNKNOWN** | gate NOT_REQUIRED, claim present; dispatch codex, completed; tokens 6,708,692 |
| `agent_crew` / `7057b1d7` | `impl-shadow-block-precision` | failed | **UNKNOWN** | gate NOT_REQUIRED, claim present; dispatch codex, failed; tokens unreported |
| `agent_crew` / `ac571342` | `impl-shadow-block-precision-2` | in_progress | **UNKNOWN** | gate NOT_REQUIRED, claim present; dispatch codex, in_progress; tokens unreported |
| `alfred` / `982e6444` | `review-owner-mem-pr57` | completed / request_changes | **UNKNOWN** | gate NOT_REQUIRED, claim present; dispatch claude, completed; tokens 2,550,301 |
| `alfred` / `305b21d0` | `fix-review-impl-adr-owner-approval-scope-2-r0-r1` | failed | **UNKNOWN** | gate NOT_REQUIRED, no claim in task; dispatch codex, failed; tokens unreported |
| `alfred` / `ce50e3fb` | `impl-capreg-memory-existing` | completed | **UNKNOWN** | gate NOT_REQUIRED, claim present; dispatch codex, completed; tokens 16,134,289 |
| `alfred` / `1408cddf` | `impl-shadow-expiry-registry` | failed | **UNKNOWN** | gate NOT_REQUIRED, claim present; dispatch codex, failed; tokens unreported |
| `alfred` / `6ae408d5` | `impl-shadow-expiry-registry-2` | in_progress | **UNKNOWN** | gate NOT_REQUIRED, claim present; dispatch codex, in_progress; tokens unreported |
| `quota-ops` / `466b4133` | `fix-review-impl-lemmalog-into-quota-ops-2-r0-r1` | failed | **UNKNOWN** | gate NOT_REQUIRED, no claim in task; dispatch codex, failed; tokens unreported |

### `REVIEW:IDENTITY_UNVERIFIED_REVIEW_REQUIRED` (10 sampled of 36)

| Project / receipt | Task | Exact outcome | Label | Evidence |
|---|---|---|---|---|
| `agent_crew` / `4e1aaff2` | `p01-canary-378-impl` | completed | **CORRECT_BLOCK** | identity UNVERIFIED, shared-UID downgrade; dispatch codex, completed; tokens 578,881 |
| `agent_crew` / `0336794a` | `p01-378-fix-r1` | completed | **CORRECT_BLOCK** | identity UNVERIFIED, shared-UID downgrade; dispatch codex, completed; tokens 6,694,092 |
| `alfred` / `e6b357e9` | `impl-adr-owner-approval-scope` | cancelled | **CORRECT_BLOCK** | identity UNVERIFIED, shared-UID downgrade; dispatch codex, cancelled; tokens unreported |
| `alfred` / `449c4638` | `impl-adr-owner-approval-scope-2` | completed | **CORRECT_BLOCK** | identity UNVERIFIED, shared-UID downgrade; dispatch codex, completed; tokens 3,995,851 |
| `alpha_engine` / `d93a3ca2` | `impl-a1-grandfather` | completed | **CORRECT_BLOCK** | identity UNVERIFIED, shared-UID downgrade; dispatch codex, completed; tokens 150,090,857 |
| `alpha_engine` / `61f2c8d8` | `fix-review-impl-a1-grandfather-r0-r1` | completed | **CORRECT_BLOCK** | identity UNVERIFIED, shared-UID downgrade; dispatch codex, completed; tokens 155,122,889 |
| `halla` / `01a950cf` | `impl-fd0822fc` | completed | **CORRECT_BLOCK** | identity UNVERIFIED, shared-UID downgrade; dispatch codex, completed; tokens 8,908,980 |
| `halla` / `6564d358` | `impl-cec8245e` | completed | **CORRECT_BLOCK** | identity UNVERIFIED, shared-UID downgrade; dispatch codex, completed; tokens 9,683,813 |
| `quota-ops` / `fb6c0abf` | `impl-lemmalog-into-quota-ops` | failed | **CORRECT_BLOCK** | identity UNVERIFIED, shared-UID downgrade; dispatch codex, failed; tokens unreported |
| `quota-ops` / `2294ecdb` | `impl-lemmalog-into-quota-ops-2` | completed | **CORRECT_BLOCK** | identity UNVERIFIED, shared-UID downgrade; dispatch codex, completed; tokens 12,996,021 |

## Reproduction

For each copied database: `SELECT receipt_id, task_id, decision, json_extract(reason, '$.code') FROM authorization_receipts WHERE seq=0;`. Join `tasks` on **both** task ID and receipt ID, then join `task_attribution` by task ID only after confirming the exact receipt match. For STOP cases, find the latest `runtime_state_events` row at or before `issued_at`. For completed-intent cases, compare earlier `intent_hash` receipts and both tasks’ pinned `context.reviewed_sha` values. The `/tmp` snapshots are local evidence and are not committed because they contain task context and provider metadata.
