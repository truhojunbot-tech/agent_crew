# SEV-0 CEA step 3 — input providers, authz broker, memory-backed reuse

- **Kind:** `result` + `discovery` + `blocker` (§4). Task `sev0-cea-lineage-s3-providers-broker-memory-par`.
- **Contract:** alfred `sev0/e11-adr-draft` @ `6cbce565` (Π P1, P2a, P6, P7, §5, §6, O9, O21b); receipt schema alfred `sev0/cea-alfred-lineage` @ `e1063eb`.
- **Branch:** `sev0/cea-lineage-s3` from `f1aee1d` (head of `sev0/cea-lineage`). New modules only; `queue.py`, `server.py`, `engine.py` and the validator call sites are untouched (s2b/s2c lane). s4 merges.

## 1. What landed

| Item | Where |
|---|---|
| L2/L3 client for alfred `tools/admission_inputs.py` (stdin/stdout, `admission-inputs/v1`; missing/timeout/bad JSON ⇒ `InputUnavailable`) | `cea/input_providers/admission_inputs.py` |
| E4 `lookup(intent) → {matches, owner, generation, hash}`; UNAVAILABLE ⇒ `available=False`, DEGRADED / stale record / unknown declared capability ⇒ `stale=True` | `cea/input_providers/capability.py` |
| canonical policy snapshot reader: generation, `sha256:` hash over canonical body, signature hook (`verifier`), no verifier ⇒ `UNKEYED` ⇒ engine BLOCK; O20 max age | `cea/input_providers/snapshot.py` |
| P6 runtime row, read-only (`mode=ro`), any failure ⇒ `read_failed` ⇒ STOPPED | `cea/input_providers/runtime.py` |
| J6 budget from Qouta `quota_cache.json` + cooldown file; O9: stale paid/overage ⇒ EXHAUSTED, stale plan ⇒ CONSTRAINED with stale `observed_at` on the receipt | `cea/input_providers/budget.py` |
| J8 gate from snapshot records only (GRANTED needs a decision record in the same snapshot) | `cea/input_providers/gate.py` |
| J-memory: L3 tighten-only gate + known-duplicate REUSE match, wired through the existing `capabilities=` / `gates=` provider slots | `cea/memory.py` |
| executor-binding authz broker (SO_PEERCRED + `/proc/<pid>/stat` start_time + `(receipt_id, attempt)`; per-registration nonce in memory only; ptrace_scope ≥ 1; refuses descendants of registered executors; only writer of `executor_binding_status=VERIFIED`; caller role always UNVERIFIED) | `cea/broker.py` |
| launcher (`--check`, `--degraded`) + systemd template | `tools/cea/broker-launch.sh`, `tools/cea/crew-authz-broker.service` |
| tests (26) + permanent memory fixture | `tests/unit/test_sev0_cea_s3.py`, `tests/fixtures/cea_memory/known_duplicate.admission_inputs.json` |

**Install step (alfred side, owner action):** copy `tools/cea/broker-launch.sh` byte-identical to
`/home/truhojun/alfred/tools/cea/broker-launch.sh` (mode `0755`, owner `truhojun`). The sudoers rule
(`/etc/sudoers.d/crew-authz-broker`) fixes that path; do not edit sudoers.

## 2. Not done in this lane (s4)

- The dispatcher does not yet call `BrokerClient.register(pid, start_time_of(pid), receipt_id, attempt)` after the one-shot spawn (`server.py` `_dispatch_task`); this lane may not edit `server.py`.
- Nothing consumes the broker's attestation yet: the engine still stamps `executor_binding_status=UNVERIFIED` unconditionally, which is correct until s4 routes the attestation into the receipt at execute start (P2 `validate_execute_start`).
- `get_engine()` default wiring still uses the Unavailable* providers; s4 switches it to `input_providers` + `memory_providers`.

## 3. Findings

1. **`blocker` — the sudoers path is not reachable by `crew-authz`.** `/home/truhojun` is `0750 truhojun:truhojun` and uid 998 is only in group 998, so `sudo -u crew-authz /home/truhojun/alfred/tools/cea/broker-launch.sh` cannot traverse to the file, and the broker cannot import agent_crew from a checkout under the home dir. Measured 2026-09-23 with `stat -c '%A %U' /home/truhojun` → `drwxr-x--- truhojun`; `id crew-authz` → `groups=998(crew-authz)`. Unblock (owner): a traversal ACL (`setfacl -m u:crew-authz:x /home/truhojun /home/truhojun/alfred /home/truhojun/alfred/tools /home/truhojun/alfred/tools/cea`) and a crew-authz-readable agent_crew install (launcher default `AGENT_CREW_AUTHZ_PYTHONPATH=/opt/agent_crew-authz/src`). Not changed here (shared infrastructure).
2. `crew-authz` is not in the client group, so the socket dir falls back to `0711` and the broker enforces the client uid with SO_PEERCRED. Adding `crew-authz` to group `truhojun` would allow `0710`.
3. alfred `admission_inputs.py` drops `reuse_target` from incident-memory matches; the reuse target is recovered only when `match.basis` contains `capability_id`/`reuse_target`. A scope-anchor-only duplicate still gates (PENDING) but records no `matched_capability`.
4. alfred has no `governance/control_policy_snapshot.json` yet and no producer key, so with these providers every admission BLOCKs on `policy_snapshot` (fail closed, as P7 requires).
5. No #308 cooldown store exists in agent_crew source; the budget provider reads `AGENT_CREW_CEA_COOLDOWN_FILE` (`{provider: until_epoch}`), absent ⇒ no cooldown recorded.
