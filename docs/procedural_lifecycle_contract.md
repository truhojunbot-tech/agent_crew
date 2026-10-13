# Procedural lifecycle contract — `agent_crew.procedural_lifecycle/v1` (#716)

Status: first deliverable of #716 — the contract, the public interface, and the
active-reader filter. This is code completion only. No consumer deployment is
covered here, and no runtime PASS can be inferred from merging it.

## 1. Storage: the existing row, no new store

A candidate is **one row** in the existing `adr001_memory` table owned by
`memory_runtime.SQLiteMemoryStorage` (and its `HybridMemoryStorage` subclass).
Nothing new is added to the schema, there is no second database, and there is
no JSONL file. Episodes, decisions and tasks that already exist are referenced
as evidence. They are not copied.

| `adr001_memory` column | v1 value |
|---|---|
| `layer` | `procedural` (an existing layer) |
| `key` | `procedure:<candidate_id>` |
| `scope` | `MemoryScope(fleet=<fleet or "">, project=<project>)`, canonical JSON |
| `version` | `1` at create, then +1 on each transition (compare-and-set) |
| `created` | time of the last write (unchanged runtime semantics) |
| `value` | JSON object, shown below |
| `superseded_by`, `invalidated_at` (hybrid only) | left `NULL`. Lifecycle supersession lives in `value.lifecycle` so the default backend, which has no such columns, behaves the same way |

```jsonc
{
  "kind": "procedure_candidate",         // marks a lifecycle row
  "title": "...", "text": "<rule>",      // existing readers render these
  "source_ref": "...",                    // existing provider's source_ref
  "lifecycle": {
    "contract": "agent_crew.procedural_lifecycle/v1",
    "candidate_id": "cand-<sha256(project \0 source_event_id)[:16]>",
    "source_event_id": "...",            // stable source identity (idempotency key)
    "project": "...",                     // project scope (also in row scope)
    "origin": "project_procedure" | "owner_principle",
    "decision_ref": "...",                // originating decision reference
    "source_ref": "...",                  // originating source reference
    "evidence": [{"kind","ref","uri","excerpt"}],   // procedural_memory.Evidence
    "scope": {"repos","paths","modules","task_types","roles"},  // procedural_memory.Scope
    "rule": {"title","rule","pattern","trigger","required_action","prohibited_action"},
    "created_at": 0.0,
    "status": "candidate_lesson" | "active_procedure" | "rejected_lesson" | "deprecated_procedure",
    "due_at": 0.0,
    "disposition": null | {"action": "promote"|"reject"|"hold"|"superseded",
                           "by", "role", "at", "reason"},
    "missing_evidence": ["..."],         // set by hold, cleared by promote
    "supersedes": "<candidate_id>" | "",
    "superseded_by": "<candidate_id>" | "",
    "procedure": null | { /* procedural_memory.Procedure.to_dict() */ },
    "history": [{"at","action","from","to","by","reason", ...}]  // append-only
  }
}
```

### Field mapping required by #716

| #716 field | Where it is stored |
|---|---|
| stable candidate/source identity | `lifecycle.candidate_id` (derived from project + `source_event_id`) and row `key` |
| project scope | row `scope.project` and `lifecycle.project` |
| originating decision / evidence refs | `lifecycle.decision_ref`, `lifecycle.source_ref`, `lifecycle.evidence[]` |
| creation time | `lifecycle.created_at` |
| lifecycle status | `lifecycle.status` (constants from `procedural_memory`) |
| due time | `lifecycle.due_at` |
| disposition / approver / time | `lifecycle.disposition` plus a `history[]` entry |
| supersession | `lifecycle.supersedes` / `lifecycle.superseded_by`, with the old row `deprecated_procedure` |
| missing-evidence refs | `lifecycle.missing_evidence` |

## 2. Lifecycle: reuses `procedural_memory`

```
create_at_decision ─▶ candidate_lesson ──promote──▶ (validated_lesson) ─▶ active_procedure
                                       ──reject───▶ rejected_lesson
                                       ──hold─────▶ candidate_lesson  (+missing_evidence, new due_at)
active_procedure ──successor promoted with supersedes──▶ deprecated_procedure
```

- Each transition is checked with `procedural_memory.can_transition`. Promotion
  passes through `validated_lesson` (both steps are recorded in `history`).
- Promotion runs `procedural_memory.validate_candidate` (evidence resolves, a
  global scope needs at least `MIN_EVIDENCE_FOR_BROAD_SCOPE` = 2 items, there is
  a trigger, the rule is actionable, it does not contradict authoritative text,
  and it does not conflict with an active procedure on the same trigger). It then
  calls `procedural_memory.promote`, so the new procedure starts in `shadow` with
  the existing TTL and review defaults.
- **Hold** does not change the status. The row stays `candidate_lesson`, so it
  stays inactive. A hold must name the missing evidence, and its `recheck_at`
  must fall in `(now, now + DuePolicy.max_recheck_seconds]`. A recheck is a later
  `promote_candidate(..., additional_evidence=[...])`.
- **Supersession**: the successor names `supersedes=<candidate_id>` at create.
  Promoting it deprecates the old active row in the **same SQLite transaction**
  (`SQLiteMemoryStorage.compare_and_set`), using `procedural_memory.deprecate`.
- A refused call writes nothing and returns `ok=False` with `reasons`.

### Approver contract

`Approver(identity, role, project, authenticated)`. The consumer authenticates
the identity and sets `authenticated=True`. The core checks:

| origin | required role |
|---|---|
| `project_procedure` | `project_coordinator` of the same project |
| `owner_principle` | `owner` of the same project |

An unauthenticated actor, an empty identity, a different project or a different
role is refused. A bare string is refused too, because `promote()`'s
`approved_by` alone is not enough here. The core contains no deployment bot
names, addresses or credentials.

### Due contract

`DuePolicy(initial_seconds=86400, max_recheck_seconds=86400)` is supplied by the
consumer. The defaults are the 24h initial deadline from #716 and a recheck
bound of the same length. The core has no scheduler: `due_candidates(now=...)`
is a deterministic read for an external due scanner.

## 3. Public interface — `src/agent_crew/procedural_lifecycle.py`

| Function | Purpose |
|---|---|
| `create_at_decision(storage, project, source_event_id, decision_ref, source_ref, title, rule, trigger, evidence, scope, ..., origin, supersedes, due_policy)` | Idempotent create. A re-delivery returns `action="duplicate"` and the same row; the first write wins at the primary key |
| `promote_candidate(storage, project, candidate_id, approver, additional_evidence=None, authoritative_text="")` | candidate → validated → active (+ supersession) |
| `reject_candidate(storage, project, candidate_id, approver, reason)` | → `rejected_lesson`; needs a reason |
| `hold_candidate(storage, project, candidate_id, approver, missing_evidence, recheck_at, due_policy)` | records missing evidence and a new `due_at` |
| `pending_candidates(storage, project, reviewer)` | unresolved (incl. held) rows. Returns nothing for an unauthorized or other-project reviewer |
| `due_candidates(storage, project, reviewer, now)` | pending rows with `due_at <= now` |
| `candidate(storage, project, candidate_id, reviewer)` | one row, any status, with history |
| `active_procedures(storage, project)` / `active_rows(...)` | active view through the same filter as every live reader; returns `procedural_memory.Procedure` objects for `match_procedures` |
| `TransitionOutcome.to_dict()` | exported, auditable outcome for an external decision-post adapter |

Every call takes `fleet=""` and reads and writes that fleet exactly. Project and
fleet equality is strict, so a fleet-ancestor row or another project's row is
never a candidate.

## 4. Active-reader filter (production paths)

`memory_runtime.procedural_row_servable` is the single rule, and it is the
authority. A procedural row that has the lifecycle marker (`kind ==
"procedure_candidate"` or any `lifecycle` key) is served **only** if
`memory_runtime.servable_procedure` accepts it: `lifecycle.status ==
"active_procedure"`, the stored procedure passes the shared validator
`procedural_memory.procedure_from_stored` (required fields, every field's
dataclass type, a scope with exactly the `Scope` keys holding lists of text,
well-formed evidence, no unknown fields), its `state` is active and its
`expires_at` is still ahead. Any failure means not served; a row that claims to
be active but fails is logged once. Readers never raise on stored data.

`procedural_servable_sql` is only a **coarse prefilter** (status, required
field types, expiry). It may admit a row the validator rejects (for example a
string `scope`), so every SQL read path also filters its rows in Python.

| Reader | File / function | How the rule is applied |
|---|---|---|
| live context reconstruction | `memory_runtime.reconstruct_context` → `retrieve` | Python filter in `SQLiteMemoryStorage.retrieve` |
| live dispatch block | `memory_runtime.dispatch_memory_block` → `retrieve` | same |
| owner-statement resolution | `memory_runtime._scoped_owner_records` → `retrieve` | same |
| shadow retrieval | `SQLiteMemoryStorage.retrieve_shadow` | SQL prefilter in `base`, then Python filter |
| shadow provider fallback | `RuntimeMemoryProvider.retrieve` (non-SQLite storage) → `retrieve` | same as `retrieve` |
| hybrid head / candidates / render-time rejoin | `memory_hybrid._effective_sql` + `memory_hybrid._servable` (in `render_head`, `_candidates`, `effective_rows`, `retrieve_ranked`) | SQL prefilter, then Python filter |
| public lifecycle view | `procedural_lifecycle.active_rows` / `active_procedures` | Python filter; invalid rows skipped |

Only the lifecycle API passes `retrieve(..., include_inactive_procedures=True)`.

The Context Pack's procedure source (`context_pack._match_procedures`) reads the
#240 JSONL file through `procedural_memory.match_procedures`, which already
serves only `ACTIVE` and unexpired procedures. This PR leaves it alone. A
consumer that wants the row-backed set calls `active_procedures()`.

### Legacy compatibility rule (v1)

- A procedural row **without** the lifecycle marker was written before this
  contract, for example `owner_principle:*` rows. It is served exactly as before.
  These rows were already authorized by their writer, and this PR changes nothing
  for them.
- A row **with** the marker but without a valid active status is not served.
  A missing status is never treated as validation.
- Out of scope for v1: tightening legacy marker-less procedural rows other than
  `owner_principle:*`. Existing tests and producers write such rows. Gating them
  needs an explicit migration decision, and this PR does not make it.

## 5. Rollback

Revert `procedural_lifecycle.py` and the reader-filter hunks. Stored lifecycle
rows stay in place for audit. After a revert, readers would serve marked rows
again, so a rollback must keep the filter: `procedural_row_servable` (with
`procedural_servable_sql` as its prefilter). Never restore a read path that treats candidate rows
as active procedures.

## 6. Tests

`tests/unit/test_issue_716_procedural_lifecycle.py` runs against a real
temp-DB `SQLiteMemoryStorage` and `HybridMemoryStorage`. It covers create,
duplicate delivery, approve, reject, hold/recheck, supersede, restart
round-trip, cross-project read = 0, broad-scope single-evidence refusal,
unqualified approver, missing source, a marker row without status, legacy
principle serving, expiry, malformed active rows (one parametrized case per
broken field: id, version, title, rule, scope shape, evidence, expiry type,
lifecycle status; each asserts not served, no raise, valid rows still served
on both backends and the hybrid path), a lost compare-and-set, and a consumer adapter that
uses only the public interface. No layer or validation is monkeypatched. The
only `monkeypatch` is the env flag that turns on `reconstruct_context`.
