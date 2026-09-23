# SEV-0 E8(a): §11 adversarial suite, red baseline

Measured 2026-09-23T07:53Z on base `5efea31` (agent_crew `origin/main`), in
the worker worktree, against fixtures only. Branch `sev0/e8-adversarial-suite`.

- **Source.** Directive §11 (alfred
  `governance/incidents/qouta-tokenomics-sev0-incident-directive.md`); gap map
  E8 (alfred `sev0/decision-package-v1` @`ccb9485`); strand B (alfred#51
  c5776412757).
- **Suite.** `tests/unit/test_sev0_e8_adversarial.py`.

```
python3 -m pytest tests/unit/test_sev0_e8_adversarial.py -rxX            # baseline: 3 passed, 11 xfailed
python3 -m pytest tests/unit/test_sev0_e8_adversarial.py --runxfail      # shows each failure's reason
```

## What "pass" means

§11 passes only when the ecosystem, not Qouta alone, discovers the conflict
and prevents unauthorized duplicate implementation.

Each scenario sends the request to the real admission entry point,
`POST /tasks`. The fixtures give the ecosystem the knowledge it needs: its own
task history, a capability registry, or both. The scenario passes only if the
duplicate is:
- **not admitted:** no 201;
- **not runnable:** no pending or in-progress row;
- **not pushed:** nothing sent to a pane.

The suite asserts outcomes only. How the block is expressed (status code,
wording) is left to E5.

Red tests are `xfail(strict=True)`. When one starts passing, pytest reports
XPASS and the suite fails, so the baseline has to be updated on purpose.

## Results

| # | Scenario (§11) | Knowledge the ecosystem holds in the fixture | Observed today (`--runxfail`) | Status | Green when |
|---|---|---|---|---|---|
| S1 | Existing capability presented as new | completed task for the capability in the queue, plus the registry | `duplicate admitted: 201` | XFAIL | E4 + E5 |
| S2 | ADR context omitted | registry (ADR-001); the request names no ADR or capability | `duplicate admitted: 201` | XFAIL | E4 + E5 |
| S3 | Owner not called | registry names owner `quota-core`; request declares the capability, owner never consulted | `duplicate admitted: 201` | XFAIL | E4 + E5(a)/(d) |
| S4 | Session restart / compaction | the same issue in flight, then a new server process on the same DB | `duplicate admitted: 201`, `in_flight_for_issue: ['s4-a']`: **#294 detected it and admitted anyway (advisory by design)** | XFAIL | E5(a) |
| S5 | Renamed equivalent capability | registry aliases, plus queue history under the old name | `duplicate admitted: 201` | XFAIL | E4 |
| S6 | Same capability in another repo | registry: implemented in `example/quota-core`; the request is for `alpha_engine` | `duplicate admitted: 201` | XFAIL | E4 |
| S7 | Qouta silent / unavailable | registry path unreachable; the queue's own history still has the capability | `duplicate admitted: 201` | XFAIL | E4/E5 without Qouta; G13 fail-closed (→ E9) |
| S8 | Claude quota exhausted | queue history, registry, and the provisional `AGENT_CREW_PROVIDER_EXHAUSTED=claude` signal | `duplicate admitted: 201` | XFAIL | G16 + E4/E5 |
| S9a | Stale registry | registry generation `2026-09-19.1`, empty; queue history has the capability | `duplicate admitted: 201` | XFAIL | E4 §9.2 + G13 |
| S9b | Partial registry | registry entry without `owner` and `adr`; queue history has the capability | `duplicate admitted: 201` | XFAIL | E4 §9.2 + G13 |
| SB | Strand B: CLAIMED/RUNNING provable from the queue | task admitted and pushed | `CLAIMED has no timestamp in the queue` | XFAIL | G12 / D6 |
| C1 | Control: an "instructed" task that was never enqueued | none | 404 from `GET /tasks/{id}` | PASS | stays PASS |
| C2 | Control: a genuinely new capability | registry has nothing related | 201, pushed to the owned pane | PASS | stays PASS |
| C3 | Control: review of existing work on the same capability | in-flight implement task | 201 | PASS | stays PASS |

**Totals: §11 has 0 of 9 scenarios passing (10 cases with S9's two
variants), strand B has 0 of 1, and all 3 controls pass.**

Every XFAIL fails at the intended assertion, not in setup. I checked this with
`--runxfail`: ten fail with "duplicate admitted: 201" and strand B fails with
"CLAIMED has no timestamp".

## Provisional parts, to align with E4, G12 and G16

No capability registry is consulted on the dispatch path today. So the suite
uses stand-ins, and each one needs replacing when its lane lands:

| Stand-in | Replace with |
|---|---|
| Registry JSON `{generation, capabilities[{id, aliases, owner, repo, adr, status}]}`, pointed to by `AGENT_CREW_CAPABILITY_REGISTRY` | The E4 source of truth (the alfred#4 registry and the W3 schema). Change `write_registry()` only; the scenarios assert outcomes and do not change. |
| `AGENT_CREW_PROVIDER_EXHAUSTED` | The G16 provider-budget signal |
| Strand B's `execution.{claimed_at, claimed_by_role, dispatched_at, dispatch_target}` | The G12 field names as merged |

## Scope

- **Covered:** the agent_crew admission path (`POST /tasks`) only.
- **Not covered:**
  - the `crew triage --watch` admission path (`watch.py`);
  - Alfred's coordinator preflight (alfred#47 / G13).

  §11 names the ecosystem, so both need scenarios of their own.
- **Data:** no production data. Every repo, owner, ADR and capability id is
  synthetic.
