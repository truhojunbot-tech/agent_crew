"""#716 — procedural lifecycle on the existing ADR-001 procedural rows.

`procedural_memory` owns the rules: lifecycle states, legal transitions,
`validate_candidate`, `promote`, and the explicit-approver requirement. This
module only stores that lifecycle on the ``adr001_memory`` table that
`memory_runtime` already owns, so there is one canonical row per candidate and
no second store. The payload contract is versioned in
``docs/procedural_lifecycle_contract.md``.

    create_at_decision -> candidate_lesson --promote--> active_procedure
                                           --reject---> rejected_lesson
                                           --hold-----> candidate_lesson (+missing evidence, new due_at)
    active_procedure --superseded by a promoted successor--> deprecated_procedure

⛔Nothing here makes a row active without `validate_candidate` and a qualified,
  authenticated approver. Active readers in `memory_runtime`/`memory_hybrid`
  serve a lifecycle row only while its status is active.

⛔This is the public core. It does not schedule, post or notify. A consumer
  adapter takes the exported `TransitionOutcome` and the due read API and
  does that itself, under its own deployment policy.
"""
from __future__ import annotations

import copy
import hashlib
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from . import procedural_memory as pm
from .memory_runtime import (
    PROCEDURE_CANDIDATE_KIND, MemoryRecord, MemoryScope, SQLiteMemoryStorage,
    procedural_row_servable, servable_procedure,
)

CONTRACT_VERSION = "agent_crew.procedural_lifecycle/v1"
KEY_PREFIX = "procedure:"
LAYER = "procedural"

# ── approver contract ─────────────────────────────────────────────────
ROLE_PROJECT_COORDINATOR = "project_coordinator"
ROLE_OWNER = "owner"
REVIEWER_ROLES = frozenset({ROLE_PROJECT_COORDINATOR, ROLE_OWNER})

#: Where a candidate came from decides who may dispose of it.
ORIGIN_PROJECT = "project_procedure"
ORIGIN_OWNER = "owner_principle"
_REQUIRED_ROLE = {ORIGIN_PROJECT: ROLE_PROJECT_COORDINATOR, ORIGIN_OWNER: ROLE_OWNER}

# ── dispositions / outcome actions ────────────────────────────────────
ACTION_CREATE = "create"
ACTION_DUPLICATE = "duplicate"
ACTION_PROMOTE = "promote"
ACTION_REJECT = "reject"
ACTION_HOLD = "hold"
ACTION_SUPERSEDED = "superseded"

#: Defaults for the consumer-supplied due contract: 24h to the first
#: disposition, and a hold may push the deadline out by at most this much.
DEFAULT_INITIAL_DUE_SECONDS = 24 * 3600
DEFAULT_MAX_RECHECK_SECONDS = 24 * 3600


@dataclass(frozen=True)
class Approver:
    """Who is acting. The consumer authenticates; this core only checks the claim.

    ⛔``authenticated`` must be set by the consumer after it verified the
      identity. An unverified actor is refused even with the right role.
    """

    identity: str
    role: str
    project: str
    authenticated: bool = False


@dataclass(frozen=True)
class DuePolicy:
    initial_seconds: float = DEFAULT_INITIAL_DUE_SECONDS
    max_recheck_seconds: float = DEFAULT_MAX_RECHECK_SECONDS

    def __post_init__(self):
        if not (self.initial_seconds > 0 and self.max_recheck_seconds > 0):
            raise ValueError("due policy windows must be positive")


@dataclass
class TransitionOutcome:
    """Auditable result of one lifecycle call, for an external adapter.

    Returned for refusals too: ``ok=False`` with ``reasons`` and no write.
    """

    contract: str
    action: str
    ok: bool
    project: str
    candidate_id: str
    key: str
    from_status: str = ""
    to_status: str = ""
    version: int = 0
    at: float = 0.0
    by: str = ""
    due_at: float = 0.0
    missing_evidence: list = field(default_factory=list)
    superseded: str = ""
    reasons: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CandidateRow:
    """One lifecycle row as stored. ``lifecycle`` is the contract payload."""

    key: str
    project: str
    version: int
    value: dict

    @property
    def lifecycle(self) -> dict:
        return self.value.get("lifecycle") or {}

    @property
    def candidate_id(self) -> str:
        return str(self.lifecycle.get("candidate_id") or "")

    @property
    def status(self) -> str:
        return str(self.lifecycle.get("status") or "")

    @property
    def due_at(self) -> float:
        return float(self.lifecycle.get("due_at") or 0.0)

    @property
    def held(self) -> bool:
        return (self.status == pm.CANDIDATE
                and (self.lifecycle.get("disposition") or {}).get("action") == ACTION_HOLD)


# ── identity ──────────────────────────────────────────────────────────


def candidate_id_for(project: str, source_event_id: str) -> str:
    """Stable identity: the same source event in the same project is one candidate."""
    digest = hashlib.sha256(f"{project}\0{source_event_id}".encode()).hexdigest()[:16]
    return f"cand-{digest}"


def candidate_key(candidate_id: str) -> str:
    return KEY_PREFIX + candidate_id


# ── qualification ─────────────────────────────────────────────────────


def approver_reasons(approver, *, project: str, origin: str) -> list:
    """Why this actor may not dispose of a candidate of ``origin`` in ``project``."""
    if not isinstance(approver, Approver):
        return ["an explicit Approver is required"]
    reasons = []
    if not approver.identity:
        reasons.append("approver identity is empty")
    if not approver.authenticated:
        reasons.append("approver identity is not authenticated")
    if approver.project != project:
        reasons.append("approver is not scoped to this project")
    required = _REQUIRED_ROLE.get(origin)
    if required is None:
        reasons.append(f"unknown candidate origin {origin!r}")
    elif approver.role != required:
        reasons.append(f"{origin} requires role {required}, not {approver.role or 'none'}")
    return reasons


def reviewer_may_read(reviewer, project: str) -> bool:
    return (isinstance(reviewer, Approver) and bool(reviewer.identity)
            and reviewer.authenticated and reviewer.project == project
            and reviewer.role in REVIEWER_ROLES)


# ── storage helpers ───────────────────────────────────────────────────


def _require_storage(storage) -> SQLiteMemoryStorage:
    if not isinstance(storage, SQLiteMemoryStorage):
        raise TypeError("procedural lifecycle needs the ADR-001 SQLite memory storage")
    return storage


def _scope(project: str, fleet: str) -> MemoryScope:
    if not project:
        raise ValueError("project is required")
    return MemoryScope(fleet=fleet, project=project)


def _rows(storage, project: str, fleet: str, exact_key: str = "") -> list:
    storage = _require_storage(storage)
    records = storage.retrieve(_scope(project, fleet), exact_key=exact_key,
                               include_inactive_procedures=True)
    # Strict project and fleet equality: a fleet ancestor or another project's
    # row is never a candidate of this project.
    return [CandidateRow(r.key, r.scope.project, r.version, r.value) for r in records
            if r.layer == LAYER and r.scope.project == project and r.scope.fleet == fleet
            and r.key.startswith(KEY_PREFIX)
            and r.value.get("kind") == PROCEDURE_CANDIDATE_KIND
            and isinstance(r.value.get("lifecycle"), dict)]


def _load(storage, project: str, fleet: str, candidate_id: str) -> Optional[CandidateRow]:
    rows = _rows(storage, project, fleet, candidate_key(candidate_id))
    return rows[0] if rows else None


def _evidence_list(items) -> list:
    out = []
    for item in items or []:
        e = item if isinstance(item, pm.Evidence) else pm.Evidence(**dict(item))
        out.append(e)
    return out


def _scope_obj(raw) -> pm.Scope:
    if isinstance(raw, pm.Scope):
        return raw
    return pm.Scope(**dict(raw or {}))


def _as_lesson(lifecycle: dict, extra_evidence: Optional[list] = None) -> pm.CandidateLesson:
    rule = lifecycle.get("rule") or {}
    return pm.CandidateLesson(
        lesson_id="lesson-" + str(lifecycle.get("candidate_id", "")).removeprefix("cand-"),
        title=str(rule.get("title") or ""), rule=str(rule.get("rule") or ""),
        pattern=str(rule.get("pattern") or ""),
        scope=_scope_obj(lifecycle.get("scope")),
        evidence=_evidence_list(lifecycle.get("evidence")) + list(extra_evidence or []),
        trigger=dict(rule.get("trigger") or {}),
        required_action=str(rule.get("required_action") or ""),
        prohibited_action=str(rule.get("prohibited_action") or ""),
        state=str(lifecycle.get("status") or pm.CANDIDATE),
    )


def _procedure_from(raw: dict) -> pm.Procedure:
    """Stored procedure dict -> `Procedure` through the shared strict validator."""
    return pm.procedure_from_stored(raw)


def _outcome(action: str, row_project: str, candidate_id: str, **kw) -> TransitionOutcome:
    return TransitionOutcome(contract=CONTRACT_VERSION, action=action,
                             project=row_project, candidate_id=candidate_id,
                             key=candidate_key(candidate_id) if candidate_id else "", **kw)


def _refuse(action: str, project: str, candidate_id: str, reasons: list,
            row: Optional[CandidateRow] = None, by: str = "") -> TransitionOutcome:
    status = row.status if row else ""
    return _outcome(action, project, candidate_id, ok=False, reasons=list(reasons),
                    from_status=status, to_status=status,
                    version=row.version if row else 0, by=by,
                    due_at=row.due_at if row else 0.0,
                    missing_evidence=list(row.lifecycle.get("missing_evidence") or [])
                    if row else [])


def _record(project: str, fleet: str, key: str, value: dict, version: int) -> MemoryRecord:
    return MemoryRecord(LAYER, key, value, _scope(project, fleet), version)


def _history(lifecycle: dict, entry: dict) -> None:
    lifecycle["history"] = list(lifecycle.get("history") or []) + [entry]


# ── 1. idempotent create at decision ──────────────────────────────────


def create_at_decision(storage, *, project: str, source_event_id: str,
                       decision_ref: str, source_ref: str, title: str, rule: str,
                       trigger: dict, evidence: Optional[list] = None,
                       scope=None, required_action: str = "",
                       prohibited_action: str = "", pattern: str = "",
                       origin: str = ORIGIN_PROJECT, supersedes: str = "",
                       fleet: str = "", due_policy: DuePolicy = DuePolicy(),
                       now: Optional[float] = None) -> TransitionOutcome:
    """Record a candidate when a decision is made. Re-delivery returns the same row.

    ⛔A missing source event, decision reference or source reference is refused:
      a candidate that cannot be traced back is not reviewable.
    """
    candidate_id = candidate_id_for(project, source_event_id) if project and source_event_id else ""
    reasons = []
    if not project:
        reasons.append("project is required")
    for name, val in (("source_event_id", source_event_id), ("decision_ref", decision_ref),
                      ("source_ref", source_ref)):
        if not isinstance(val, str) or not val.strip():
            reasons.append(f"missing {name}")
    if origin not in _REQUIRED_ROLE:
        reasons.append(f"unknown candidate origin {origin!r}")
    try:
        ev = _evidence_list(evidence)
        rule_scope = _scope_obj(scope)
    except TypeError as exc:
        ev, rule_scope = [], pm.Scope()
        reasons.append(f"malformed evidence or scope: {exc}")
    if any(not e.resolves() for e in ev):
        reasons.append("evidence does not resolve to a durable reference")
    if reasons:
        return _refuse(ACTION_CREATE, project, candidate_id, reasons)

    existing = _load(storage, project, fleet, candidate_id)
    if existing is not None:
        return _duplicate(existing)

    t = now or time.time()
    lifecycle = {
        "contract": CONTRACT_VERSION,
        "candidate_id": candidate_id,
        "source_event_id": source_event_id,
        "project": project,
        "origin": origin,
        "decision_ref": decision_ref,
        "source_ref": source_ref,
        "evidence": [asdict(e) for e in ev],
        "scope": asdict(rule_scope),
        "rule": {"title": title, "rule": rule, "pattern": pattern,
                 "trigger": dict(trigger or {}), "required_action": required_action,
                 "prohibited_action": prohibited_action},
        "created_at": t,
        "status": pm.CANDIDATE,
        "due_at": t + due_policy.initial_seconds,
        "disposition": None,
        "missing_evidence": [],
        "supersedes": supersedes,
        "superseded_by": "",
        "procedure": None,
        "history": [{"at": t, "action": ACTION_CREATE, "from": "", "to": pm.CANDIDATE,
                     "by": "", "reason": ""}],
    }
    value = {"kind": PROCEDURE_CANDIDATE_KIND, "title": title, "text": rule,
             "source_ref": source_ref, "lifecycle": lifecycle}
    key = candidate_key(candidate_id)
    # put() never overwrites an equal-or-newer version, so a racing second
    # delivery is a no-op at the primary key; the read-back says who won.
    storage.put(_record(project, fleet, key, value, 1))
    stored = _load(storage, project, fleet, candidate_id)
    if stored is None:
        return _refuse(ACTION_CREATE, project, candidate_id, ["write was not visible"])
    if stored.value != value:
        return _duplicate(stored)
    return _outcome(ACTION_CREATE, project, candidate_id, ok=True, from_status="",
                    to_status=pm.CANDIDATE, version=stored.version, at=t,
                    due_at=lifecycle["due_at"])


def _duplicate(row: CandidateRow) -> TransitionOutcome:
    return _outcome(ACTION_DUPLICATE, row.project, row.candidate_id, ok=True,
                    from_status=row.status, to_status=row.status, version=row.version,
                    at=float(row.lifecycle.get("created_at") or 0.0), due_at=row.due_at,
                    missing_evidence=list(row.lifecycle.get("missing_evidence") or []))


# ── 2. dispositions ───────────────────────────────────────────────────


def _guard(storage, action: str, project: str, fleet: str, candidate_id: str,
           approver) -> tuple:
    row = _load(storage, project, fleet, candidate_id)
    if row is None:
        return None, _refuse(action, project, candidate_id, ["candidate not found in project"])
    origin = str(row.lifecycle.get("origin") or "")
    reasons = approver_reasons(approver, project=project, origin=origin)
    if reasons:
        return None, _refuse(action, project, candidate_id, reasons, row,
                             by=getattr(approver, "identity", ""))
    return row, None


def promote_candidate(storage, *, project: str, candidate_id: str, approver: Approver,
                      additional_evidence: Optional[list] = None,
                      authoritative_text: str = "", fleet: str = "",
                      now: Optional[float] = None) -> TransitionOutcome:
    """candidate -> validated -> active, through `validate_candidate` and `promote`.

    ``additional_evidence`` is how a held candidate is rechecked: the evidence
    the hold said was missing is supplied here and validated with the rest.
    """
    row, refused = _guard(storage, ACTION_PROMOTE, project, fleet, candidate_id, approver)
    if refused:
        return refused
    lifecycle = copy.deepcopy(row.lifecycle)
    status = row.status
    if not (pm.can_transition(status, pm.VALIDATED) and pm.can_transition(pm.VALIDATED, pm.ACTIVE)):
        return _refuse(ACTION_PROMOTE, project, candidate_id,
                       [f"cannot promote from {status}"], row, approver.identity)
    reasons = []
    for name in ("decision_ref", "source_ref", "source_event_id"):
        if not str(lifecycle.get(name) or "").strip():
            reasons.append(f"missing {name}")
    try:
        extra = _evidence_list(additional_evidence)
    except TypeError as exc:
        extra = []
        reasons.append(f"malformed additional evidence: {exc}")
    lesson = _as_lesson(lifecycle, extra)

    supersedes = str(lifecycle.get("supersedes") or "")
    target = None
    if supersedes:
        target = _load(storage, project, fleet, supersedes)
        if target is None or target.status != pm.ACTIVE \
                or not pm.can_transition(target.status, pm.DEPRECATED) \
                or _servable(target) is None:
            reasons.append(f"superseded candidate {supersedes} is not an active procedure "
                           "in this project")
    active = [p for r in active_rows(storage, project=project, fleet=fleet)
              if r.candidate_id not in (candidate_id, supersedes)
              for p in [_servable(r)] if p is not None]
    validation = pm.validate_candidate(lesson, existing=active,
                                       authoritative_text=authoritative_text, now=now)
    reasons.extend(validation.reasons)
    if reasons:
        return _refuse(ACTION_PROMOTE, project, candidate_id, reasons, row, approver.identity)

    t = now or time.time()
    procedure = pm.promote(
        lesson, approved_by=approver.identity, validation=validation,
        supersedes=(target.lifecycle["procedure"]["procedure_id"] if target else ""), now=t)
    lifecycle.update({
        "status": pm.ACTIVE,
        "evidence": [asdict(e) for e in lesson.evidence],
        "missing_evidence": [],
        "disposition": {"action": ACTION_PROMOTE, "by": approver.identity,
                        "role": approver.role, "at": t, "reason": ""},
        "procedure": procedure.to_dict(),
    })
    _history(lifecycle, {"at": t, "action": ACTION_PROMOTE, "from": status,
                         "to": pm.VALIDATED, "by": approver.identity, "reason": ""})
    _history(lifecycle, {"at": t, "action": ACTION_PROMOTE, "from": pm.VALIDATED,
                         "to": pm.ACTIVE, "by": approver.identity, "reason": ""})
    writes = [(_record(project, fleet, row.key, {**row.value, "lifecycle": lifecycle},
                       row.version + 1), row.version)]
    if target is not None:
        old = copy.deepcopy(target.lifecycle)
        deprecated = pm.deprecate(_procedure_from(old["procedure"]), by=approver.identity,
                                  reason=f"superseded by {candidate_id}", now=t)
        old.update({"status": pm.DEPRECATED, "superseded_by": candidate_id,
                    "procedure": deprecated.to_dict(),
                    "disposition": {"action": ACTION_SUPERSEDED, "by": approver.identity,
                                    "role": approver.role, "at": t,
                                    "reason": f"superseded by {candidate_id}"}})
        _history(old, {"at": t, "action": ACTION_SUPERSEDED, "from": pm.ACTIVE,
                       "to": pm.DEPRECATED, "by": approver.identity,
                       "reason": f"superseded by {candidate_id}"})
        writes.append((_record(project, fleet, target.key, {**target.value, "lifecycle": old},
                               target.version + 1), target.version))
    if not _require_storage(storage).compare_and_set(writes):
        return _refuse(ACTION_PROMOTE, project, candidate_id,
                       ["concurrent transition; re-read and retry"], row, approver.identity)
    return _outcome(ACTION_PROMOTE, project, candidate_id, ok=True, from_status=status,
                    to_status=pm.ACTIVE, version=row.version + 1, at=t,
                    by=approver.identity, due_at=row.due_at,
                    superseded=target.candidate_id if target else "")


def reject_candidate(storage, *, project: str, candidate_id: str, approver: Approver,
                     reason: str, fleet: str = "",
                     now: Optional[float] = None) -> TransitionOutcome:
    row, refused = _guard(storage, ACTION_REJECT, project, fleet, candidate_id, approver)
    if refused:
        return refused
    if not (reason or "").strip():
        return _refuse(ACTION_REJECT, project, candidate_id,
                       ["a rejection needs a reason"], row, approver.identity)
    if not pm.can_transition(row.status, pm.REJECTED):
        return _refuse(ACTION_REJECT, project, candidate_id,
                       [f"cannot reject from {row.status}"], row, approver.identity)
    t = now or time.time()
    lesson = pm.reject(_as_lesson(row.lifecycle), reason)
    lifecycle = copy.deepcopy(row.lifecycle)
    lifecycle.update({"status": lesson.state,
                      "disposition": {"action": ACTION_REJECT, "by": approver.identity,
                                      "role": approver.role, "at": t, "reason": reason}})
    _history(lifecycle, {"at": t, "action": ACTION_REJECT, "from": row.status,
                         "to": lesson.state, "by": approver.identity, "reason": reason})
    return _commit(storage, ACTION_REJECT, project, fleet, row, lifecycle, approver, t)


def hold_candidate(storage, *, project: str, candidate_id: str, approver: Approver,
                   missing_evidence: list, recheck_at: float, reason: str = "",
                   fleet: str = "", due_policy: DuePolicy = DuePolicy(),
                   now: Optional[float] = None) -> TransitionOutcome:
    """Keep a candidate inactive, record what is missing and when to look again.

    ⛔A hold is not a lifecycle state change: the row stays `candidate_lesson`
      and therefore stays out of every active reader.
    """
    row, refused = _guard(storage, ACTION_HOLD, project, fleet, candidate_id, approver)
    if refused:
        return refused
    t = now or time.time()
    reasons = []
    if row.status != pm.CANDIDATE:
        reasons.append(f"cannot hold from {row.status}")
    missing = [str(m) for m in (missing_evidence or []) if str(m).strip()]
    if not missing:
        reasons.append("a hold must name the missing evidence")
    if not (t < float(recheck_at) <= t + due_policy.max_recheck_seconds):
        reasons.append("recheck time must be in the future and within the recheck bound")
    if reasons:
        return _refuse(ACTION_HOLD, project, candidate_id, reasons, row, approver.identity)
    lifecycle = copy.deepcopy(row.lifecycle)
    lifecycle.update({"due_at": float(recheck_at), "missing_evidence": missing,
                      "disposition": {"action": ACTION_HOLD, "by": approver.identity,
                                      "role": approver.role, "at": t, "reason": reason}})
    _history(lifecycle, {"at": t, "action": ACTION_HOLD, "from": row.status,
                         "to": row.status, "by": approver.identity, "reason": reason,
                         "missing_evidence": missing, "due_at": float(recheck_at)})
    return _commit(storage, ACTION_HOLD, project, fleet, row, lifecycle, approver, t)


def _commit(storage, action: str, project: str, fleet: str, row: CandidateRow,
            lifecycle: dict, approver: Approver, t: float) -> TransitionOutcome:
    record = _record(project, fleet, row.key, {**row.value, "lifecycle": lifecycle},
                     row.version + 1)
    if not _require_storage(storage).compare_and_set([(record, row.version)]):
        return _refuse(action, project, row.candidate_id,
                       ["concurrent transition; re-read and retry"], row, approver.identity)
    return _outcome(action, project, row.candidate_id, ok=True, from_status=row.status,
                    to_status=lifecycle["status"], version=row.version + 1, at=t,
                    by=approver.identity, due_at=float(lifecycle.get("due_at") or 0.0),
                    missing_evidence=list(lifecycle.get("missing_evidence") or []))


# ── 3. read APIs ──────────────────────────────────────────────────────


def pending_candidates(storage, *, project: str, reviewer: Approver,
                       fleet: str = "") -> list:
    """Unresolved candidates (including held ones) for an authorized reviewer.

    An unauthorized or other-project reviewer reads nothing.
    """
    if not reviewer_may_read(reviewer, project):
        return []
    rows = [r for r in _rows(storage, project, fleet) if r.status == pm.CANDIDATE]
    return sorted(rows, key=lambda r: (r.due_at, r.key))


def due_candidates(storage, *, project: str, reviewer: Approver, fleet: str = "",
                   now: Optional[float] = None) -> list:
    """Pending candidates whose deadline has passed — the due scanner's input."""
    t = now or time.time()
    return [r for r in pending_candidates(storage, project=project, reviewer=reviewer,
                                          fleet=fleet) if r.due_at <= t]


def candidate(storage, *, project: str, candidate_id: str, reviewer: Approver,
              fleet: str = "") -> Optional[CandidateRow]:
    """One row with its full history, any status, for an authorized reviewer."""
    if not reviewer_may_read(reviewer, project):
        return None
    return _load(storage, project, fleet, candidate_id)


def _servable(row: CandidateRow) -> Optional[pm.Procedure]:
    """The row's validated active procedure, or None. Never raises on stored data."""
    if not procedural_row_servable(LAYER, row.value, key=row.key):
        return None
    try:
        return servable_procedure(row.value)
    except Exception:  # noqa: BLE001 - expired between the two checks
        return None


def active_rows(storage, *, project: str, fleet: str = "") -> list:
    """Active lifecycle rows, read through the same filter every live reader uses.

    Each row is re-checked with the shared validator, so a row that is not
    servable is skipped here even if a storage backend let it through.
    """
    records = _require_storage(storage).retrieve(_scope(project, fleet))
    return [CandidateRow(r.key, r.scope.project, r.version, r.value) for r in records
            if r.layer == LAYER and r.scope.project == project and r.scope.fleet == fleet
            and r.value.get("kind") == PROCEDURE_CANDIDATE_KIND
            and procedural_row_servable(LAYER, r.value, key=r.key)]


def active_procedures(storage, *, project: str, fleet: str = "") -> list:
    """The active procedure view as `procedural_memory.Procedure` objects.

    Feed it to `procedural_memory.match_procedures` like any other procedure set.
    Invalid stored rows are skipped (and logged once), never raised.
    """
    return [p for r in active_rows(storage, project=project, fleet=fleet)
            for p in [_servable(r)] if p is not None]
