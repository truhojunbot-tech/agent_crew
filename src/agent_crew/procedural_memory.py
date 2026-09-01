"""#240 — consolidate terminal task episodes into validated procedural memory.

#239 made past work *retrievable*. Retrieval alone does not stop a crew
repeating a mistake: somebody still has to notice the pattern and act on it.
This module closes the loop:

    raw_episode -> candidate_lesson -> validated_lesson -> active_procedure
                                    -> rejected_lesson       -> deprecated

Every transition is auditable, and an LLM-extracted candidate is never an
active rule. Promotion needs an explicit approver — the signature will not let
you promote without one, because "the model suggested it" is not governance.

## What this is not

⛔Git, GitHub, ADR and tests stay authoritative. Procedural memory is *derived
  governance*: it can require you to check something, never overrule current
  code, the issue's acceptance criteria, or a test. In a Context Pack that is
  enforced structurally — procedures rank below every authoritative type.

⛔A procedure does not start enforcing. It starts in `shadow`, recording what
  it *would* have required, so its false-positive rate is measured before it
  can block anything. Hard enforcement is refused until shadow data exists.

⛔One narrow incident does not become a global rule. `validate_candidate`
  rejects a broadly-scoped candidate backed by a single piece of evidence,
  which is the failure mode that makes this kind of system hated.
"""

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

PROCEDURE_SCHEMA_VERSION = 1

# ── lifecycle states ──────────────────────────────────────────────────
RAW_EPISODE = "raw_episode"
CANDIDATE = "candidate_lesson"
VALIDATED = "validated_lesson"
ACTIVE = "active_procedure"
DEPRECATED = "deprecated_procedure"
REJECTED = "rejected_lesson"

_TRANSITIONS = {
    RAW_EPISODE: {CANDIDATE},
    CANDIDATE: {VALIDATED, REJECTED},
    VALIDATED: {ACTIVE, REJECTED},
    ACTIVE: {DEPRECATED},
    DEPRECATED: set(),
    REJECTED: set(),
}

# ── enforcement modes ─────────────────────────────────────────────────
ADVISORY = "advisory"
SHADOW = "shadow"
HARD = "hard"

#: A candidate scoped this broadly needs more than one incident behind it.
_BROAD_SCOPE_KEYS = ("repos", "paths", "modules")
MIN_EVIDENCE_FOR_BROAD_SCOPE = 2


def _now() -> float:
    return time.time()


@dataclass
class Evidence:
    """A pointer to durable proof, never a copy of it."""

    kind: str  # task | issue | pr | incident | review
    ref: str
    uri: str = ""
    excerpt: str = ""

    def resolves(self) -> bool:
        return bool(self.kind and self.ref)


@dataclass
class Scope:
    """Where a rule applies. Empty everywhere == global, which is a red flag."""

    repos: list = field(default_factory=list)
    paths: list = field(default_factory=list)
    modules: list = field(default_factory=list)
    task_types: list = field(default_factory=list)
    roles: list = field(default_factory=list)

    @property
    def is_global(self) -> bool:
        return not any(getattr(self, k) for k in _BROAD_SCOPE_KEYS)

    def matches(self, *, repo: str = "", path: str = "", task_type: str = "",
                role: str = "") -> bool:
        if self.repos and repo and repo not in self.repos:
            return False
        if self.task_types and task_type and task_type not in self.task_types:
            return False
        if self.roles and role and role not in self.roles:
            return False
        if self.paths:
            if not path:
                return False
            if not any(path.startswith(p) or re.search(p, path) for p in self.paths):
                return False
        return True


@dataclass
class Procedure:
    """A versioned, auditable rule derived from evidence.

    ⛔Immutable by version. A change is a *new version* that explicitly
      supersedes the old one, so the history of what the crew was told to do
      at any point stays reconstructible.
    """

    procedure_id: str
    version: int
    title: str
    rule: str
    scope: Scope = field(default_factory=Scope)
    trigger: dict = field(default_factory=dict)
    required_action: str = ""
    prohibited_action: str = ""
    evidence: list = field(default_factory=list)
    confidence: float = 0.0
    validation_method: str = ""
    approved_by: str = ""
    state: str = CANDIDATE
    enforcement: str = SHADOW
    effective_at: float = 0.0
    review_at: float = 0.0
    expires_at: float = 0.0
    supersedes: str = ""
    conflicts_with: list = field(default_factory=list)
    exception_notes: str = ""
    history: list = field(default_factory=list)
    schema_version: int = PROCEDURE_SCHEMA_VERSION

    @property
    def key(self) -> str:
        return f"{self.procedure_id}@v{self.version}"

    def is_expired(self, now: Optional[float] = None) -> bool:
        return bool(self.expires_at) and (now or _now()) >= self.expires_at

    def needs_review(self, now: Optional[float] = None) -> bool:
        return bool(self.review_at) and (now or _now()) >= self.review_at

    def is_active(self, now: Optional[float] = None) -> bool:
        return self.state == ACTIVE and not self.is_expired(now)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["scope"] = asdict(self.scope) if isinstance(self.scope, Scope) else self.scope
        d["evidence"] = [asdict(e) if isinstance(e, Evidence) else e
                         for e in self.evidence]
        return d

    def render(self) -> str:
        """Prompt form. States plainly whether it binds or merely advises."""
        strength = ("REQUIRED" if self.enforcement == HARD else
                    "ADVISORY (shadow — being measured, not enforced)"
                    if self.enforcement == SHADOW else "ADVISORY")
        lines = [f"[{strength}] {self.title}  ({self.key})", f"  rule: {self.rule}"]
        if self.required_action:
            lines.append(f"  must: {self.required_action}")
        if self.prohibited_action:
            lines.append(f"  must not: {self.prohibited_action}")
        if self.exception_notes:
            lines.append(f"  exceptions: {self.exception_notes}")
        refs = ", ".join(f"{e.kind}:{e.ref}" for e in self.evidence[:4]
                         if isinstance(e, Evidence))
        if refs:
            lines.append(f"  because: {refs}")
        return "\n".join(lines)


@dataclass
class CandidateLesson:
    """A proposed rule plus the exact evidence that supports it."""

    lesson_id: str
    title: str
    rule: str
    pattern: str
    scope: Scope = field(default_factory=Scope)
    evidence: list = field(default_factory=list)
    trigger: dict = field(default_factory=dict)
    required_action: str = ""
    prohibited_action: str = ""
    state: str = CANDIDATE
    incomplete_reasons: list = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.incomplete_reasons


@dataclass
class ValidationResult:
    ok: bool
    reasons: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


# ── 1. episode normalisation ──────────────────────────────────────────

#: Terminal evidence a lesson may not be inferred without (#240 §1).
REQUIRED_EPISODE_FIELDS = ("task_id", "outcome")


def episode_completeness(episode: dict) -> list:
    """Why this episode is not sound enough to extract a lesson from.

    ⛔Missing outcome, missing review, or an unresolved incident must stay
      *visibly* incomplete rather than being quietly treated as evidence.
      A rule built on a task whose result we never saw is a guess wearing a
      uniform.
    """
    missing = []
    for f in REQUIRED_EPISODE_FIELDS:
        if not episode.get(f):
            missing.append(f"missing {f}")
    outcome = (episode.get("outcome") or "").lower()
    if outcome.startswith("failed") and not (episode.get("findings")
                                             or episode.get("summary")):
        missing.append("failed outcome with no findings or summary")
    if episode.get("unresolved_incident"):
        missing.append("references an unresolved incident")
    return missing


# ── 2. candidate extraction ───────────────────────────────────────────

PATTERN_REPEAT_REVIEW = "repeated_review_rejection"
PATTERN_REPEAT_FAILURE = "repeated_failure_signature"
PATTERN_NONRETRIABLE_RETRY = "retried_nonretriable_failure"
PATTERN_AC_OMISSION = "review_without_ac"

#: Runtime/provider incidents are not project procedures (#240 non-goal).
_PROVIDER_FAILURE_RE = re.compile(
    r"quota|rate.?limit|subscriber|transient|timeout|capacity", re.IGNORECASE)


def extract_candidates(episodes: list, *, min_repeats: int = 2) -> list:
    """Propose lessons from repeated evidence, never from one-offs.

    Each candidate carries the exact episodes behind it and a scope inferred
    from where those episodes actually happened — not a generalisation.
    """
    complete = [e for e in episodes if not episode_completeness(e)]
    out = []

    # Repeated identical failure signature on the same repo/branch family.
    by_sig: dict = {}
    for e in complete:
        sig = (e.get("outcome") or "").strip()
        if not sig.lower().startswith("failed"):
            continue
        by_sig.setdefault(sig, []).append(e)
    for sig, group in sorted(by_sig.items()):
        if len(group) < min_repeats:
            continue
        provider = bool(_PROVIDER_FAILURE_RE.search(sig))
        out.append(CandidateLesson(
            lesson_id=_mk_id("lesson", sig),
            title=f"Recurring failure: {sig}",
            rule=(f"Before starting work that can hit `{sig}`, check whether the "
                  f"known precondition holds; this signature recurred "
                  f"{len(group)} times."),
            pattern=(PATTERN_NONRETRIABLE_RETRY if provider
                     else PATTERN_REPEAT_FAILURE),
            scope=_scope_from(group),
            evidence=[Evidence(kind="task", ref=e["task_id"],
                               excerpt=(e.get("summary") or "")[:200])
                      for e in group],
            trigger={"outcome_signature": sig},
            required_action="verify the precondition before retrying",
            incomplete_reasons=(
                ["provider/runtime incident — not a project procedure"]
                if provider else []),
        ))

    # Repeated review rejection with the same finding text.
    by_finding: dict = {}
    for e in complete:
        for f in e.get("findings") or []:
            key = _normalise_finding(str(f))
            if key:
                by_finding.setdefault(key, []).append((e, str(f)))
    for key, group in sorted(by_finding.items()):
        if len(group) < min_repeats:
            continue
        out.append(CandidateLesson(
            lesson_id=_mk_id("lesson", key),
            title=f"Reviewers keep raising: {group[0][1][:70]}",
            rule=f"Address this before requesting review: {group[0][1][:200]}",
            pattern=PATTERN_REPEAT_REVIEW,
            scope=_scope_from([e for e, _ in group]),
            evidence=[Evidence(kind="review", ref=e["task_id"], excerpt=txt[:200])
                      for e, txt in group],
            trigger={"finding_contains": key[:60]},
            required_action="self-check this point before review",
        ))
    return out


def _normalise_finding(text: str) -> str:
    # ⛔Only the opening words form the key. Reviewers phrase the same
    #   objection with different tails ("...here too", "...anywhere"), and an
    #   exact-match key would see two unrelated one-offs instead of a pattern.
    t = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    t = re.sub(r"\s+", " ", t).strip()
    return " ".join(t.split()[:5])


def _scope_from(episodes: list) -> Scope:
    repos = sorted({e.get("repo") for e in episodes if e.get("repo")})
    roles = sorted({e.get("role") for e in episodes if e.get("role")})
    return Scope(repos=list(repos), roles=list(roles))


def _mk_id(prefix: str, basis: str) -> str:
    return f"{prefix}-{hashlib.sha256(basis.encode()).hexdigest()[:10]}"


# ── 3. validation and promotion ───────────────────────────────────────


def validate_candidate(candidate: CandidateLesson, *,
                       existing: Optional[list] = None,
                       authoritative_text: str = "",
                       now: Optional[float] = None) -> ValidationResult:
    """Deterministic gate between "the model noticed something" and a rule.

    Rejects, in order of how badly each one burns trust:
      - evidence that does not resolve;
      - a broad scope backed by a single incident (the classic over-reach);
      - no machine-detectable or clearly presentable trigger;
      - a rule contradicted by current authoritative text;
      - an already-carried incompleteness (e.g. provider incident).
    Conflicts with existing active procedures are *reported*, not resolved.
    """
    reasons, conflicts = [], []
    if candidate.incomplete_reasons:
        reasons.extend(candidate.incomplete_reasons)
    if not candidate.evidence:
        reasons.append("no supporting evidence")
    elif not all(e.resolves() for e in candidate.evidence
                 if isinstance(e, Evidence)):
        reasons.append("evidence does not resolve to a durable reference")
    if candidate.scope.is_global and len(candidate.evidence) < MIN_EVIDENCE_FOR_BROAD_SCOPE:
        reasons.append(
            f"scope is global but only {len(candidate.evidence)} incident(s) "
            f"support it — narrow the scope or gather more evidence")
    if not candidate.trigger:
        reasons.append("no machine-detectable trigger")
    if not (candidate.required_action or candidate.prohibited_action):
        reasons.append("rule is not actionable — no required or prohibited action")
    if authoritative_text and _contradicts(candidate, authoritative_text):
        reasons.append("contradicts current authoritative code/ADR text")
    for p in existing or []:
        if p.is_active(now) and _same_subject(p, candidate):
            conflicts.append(
                f"{p.key} already governs this subject — supersede it explicitly "
                f"rather than adding a second active rule")
    if conflicts:
        reasons.extend(conflicts)
    return ValidationResult(ok=not reasons, reasons=reasons, conflicts=conflicts)


def _contradicts(candidate: CandidateLesson, authoritative_text: str) -> bool:
    prohibited = (candidate.prohibited_action or "").strip().lower()
    return bool(prohibited) and prohibited in authoritative_text.lower()


def _same_subject(procedure: Procedure, candidate: CandidateLesson) -> bool:
    return (procedure.trigger or {}) == (candidate.trigger or {})


def promote(candidate: CandidateLesson, *, approved_by: str,
            validation: ValidationResult,
            validation_method: str = "deterministic_checks",
            enforcement: str = SHADOW, version: int = 1,
            supersedes: str = "", ttl_days: float = 180.0,
            review_days: float = 90.0, now: Optional[float] = None) -> Procedure:
    """Turn a validated candidate into a procedure. Requires a human/policy actor.

    ⛔`approved_by` is mandatory and `enforcement=hard` is refused here — a new
      procedure starts in shadow so its trigger accuracy is measured before it
      can block anything (#240 §5). Both are raised, not warned about, because
      a silent default is how autonomous rule-writing sneaks in.
    """
    if not approved_by:
        raise ValueError(
            "promote() requires approved_by: a candidate is never autonomously "
            "promoted into an active rule (#240)")
    if not validation.ok:
        raise ValueError(f"cannot promote an invalid candidate: {validation.reasons}")
    if enforcement == HARD:
        raise ValueError(
            "a new procedure may not start at hard enforcement — run it in "
            "shadow first and promote on measured trigger accuracy (#240 §5)")
    t = now or _now()
    return Procedure(
        procedure_id=candidate.lesson_id.replace("lesson-", "proc-"),
        version=version, title=candidate.title, rule=candidate.rule,
        scope=candidate.scope, trigger=dict(candidate.trigger),
        required_action=candidate.required_action,
        prohibited_action=candidate.prohibited_action,
        evidence=list(candidate.evidence),
        confidence=min(1.0, 0.5 + 0.1 * len(candidate.evidence)),
        validation_method=validation_method, approved_by=approved_by,
        state=ACTIVE, enforcement=enforcement,
        effective_at=t, review_at=t + review_days * 86400,
        expires_at=t + ttl_days * 86400, supersedes=supersedes,
        conflicts_with=list(validation.conflicts),
        history=[{"at": t, "to": ACTIVE, "by": approved_by,
                  "method": validation_method}],
    )


def reject(candidate: CandidateLesson, reason: str) -> CandidateLesson:
    candidate.state = REJECTED
    candidate.incomplete_reasons = list(candidate.incomplete_reasons) + [reason]
    return candidate


def deprecate(procedure: Procedure, *, by: str, reason: str,
              now: Optional[float] = None) -> Procedure:
    t = now or _now()
    procedure.state = DEPRECATED
    procedure.history = list(procedure.history) + [
        {"at": t, "to": DEPRECATED, "by": by, "reason": reason}]
    return procedure


def can_transition(src: str, dst: str) -> bool:
    return dst in _TRANSITIONS.get(src, set())


# ── 6. conflicts, versioning, decay ───────────────────────────────────


def resolve_precedence(procedures: list, *, repo: str = "",
                       now: Optional[float] = None) -> tuple:
    """`(effective, conflicts)` for a set of candidate-matching procedures.

    Precedence: a superseded version loses to its successor; a repo-specific
    procedure outranks a generic one for that repo. Anything still ambiguous
    is returned as a conflict and, per #240 §6, must stay advisory.
    """
    live = [p for p in procedures if p.is_active(now)]
    # ⛔A procedure must not supersede itself. Successive versions share a
    #   procedure_id, so matching on id alone made v2 filter itself out along
    #   with v1; only a *lower* version of the same id is superseded.
    superseded = set()
    for p in live:
        if not p.supersedes:
            continue
        for other in live:
            if other is p:
                continue
            if other.key == p.supersedes or (
                    other.procedure_id == p.supersedes
                    and other.version < p.version):
                superseded.add(other.key)
    live = [p for p in live if p.key not in superseded]

    by_trigger: dict = {}
    for p in live:
        by_trigger.setdefault(json.dumps(p.trigger, sort_keys=True), []).append(p)

    effective, conflicts = [], []
    for _trigger, group in sorted(by_trigger.items()):
        if len(group) == 1:
            effective.append(group[0])
            continue
        specific = [p for p in group if repo and repo in (p.scope.repos or [])]
        if len(specific) == 1:
            effective.append(specific[0])
            continue
        conflicts.append(
            "unresolved conflict between "
            f"{sorted(p.key for p in group)} — kept advisory until resolved")
        for p in group:
            downgraded = Procedure(**{**p.to_dict(), "scope": p.scope,
                                      "evidence": p.evidence})
            downgraded.enforcement = ADVISORY
            effective.append(downgraded)
    return effective, conflicts


def mark_stale_by_source_change(procedures: list, changed_paths: list) -> list:
    """Procedures whose scoped paths moved under them need re-review."""
    stale = []
    for p in procedures:
        for path in p.scope.paths or []:
            if any(c.startswith(path) or path in c for c in changed_paths):
                stale.append(p)
                break
    return stale


# ── matching ──────────────────────────────────────────────────────────


def match_procedures(procedures: list, *, repo: str = "", task_type: str = "",
                     role: str = "", paths: Optional[list] = None,
                     outcome_signature: str = "", findings: Optional[list] = None,
                     now: Optional[float] = None) -> list:
    """Active procedures whose scope AND trigger match this task.

    Returns `(procedure, reason)` pairs — an inclusion without a stated reason
    is not allowed into a Context Pack (#240 §4).
    """
    out = []
    for p in procedures:
        if not p.is_active(now):
            continue
        if not any(p.scope.matches(repo=repo, path=path, task_type=task_type,
                                   role=role)
                   for path in (paths or [""])):
            continue
        reason = _trigger_reason(p, outcome_signature, findings or [])
        if reason:
            out.append((p, reason))
    out.sort(key=lambda pr: (pr[0].procedure_id, pr[0].version))
    return out


def _trigger_reason(p: Procedure, outcome_signature: str, findings: list) -> str:
    trig = p.trigger or {}
    if not trig:
        return f"scope match ({p.scope.repos or 'any repo'})"
    sig = trig.get("outcome_signature")
    if sig and outcome_signature and sig == outcome_signature:
        return f"prior tasks failed with exactly `{sig}`"
    needle = trig.get("finding_contains")
    if needle and any(needle in _normalise_finding(str(f)) for f in findings):
        return f"reviewers previously raised `{needle}`"
    if not sig and not needle:
        return "scope match"
    return ""


# ── 5. shadow enforcement ─────────────────────────────────────────────


def shadow_record(procedure: Procedure, *, task_id: str, triggered: bool,
                  would: str, now: Optional[float] = None) -> dict:
    """What this procedure *would* have done, had it been enforcing.

    ⛔Recorded, never applied. Hard enforcement is only justifiable once these
      show an acceptable false-positive rate.
    """
    return {
        "schema_version": PROCEDURE_SCHEMA_VERSION,
        "at": now or _now(),
        "task_id": task_id,
        "procedure_id": procedure.procedure_id,
        "procedure_version": procedure.version,
        "enforcement": procedure.enforcement,
        "triggered": bool(triggered),
        "would": would,
    }


def shadow_metrics(records: list, *, overrides: Optional[list] = None) -> dict:
    """Trigger counts and override rate — the evidence hard mode requires."""
    overrides = overrides or []
    triggered = [r for r in records if r.get("triggered")]
    by_proc: dict = {}
    for r in triggered:
        by_proc[r["procedure_id"]] = by_proc.get(r["procedure_id"], 0) + 1
    over = len([o for o in overrides if o.get("overridden")])
    return {
        "records": len(records),
        "triggered": len(triggered),
        "trigger_rate": round(len(triggered) / len(records), 3) if records else 0.0,
        "by_procedure": by_proc,
        "overrides": over,
        "override_rate": round(over / len(triggered), 3) if triggered else 0.0,
    }


def ready_for_hard_enforcement(procedure: Procedure, metrics: dict, *,
                               min_triggers: int = 5,
                               max_override_rate: float = 0.2) -> ValidationResult:
    """⛔Refuses hard mode without measured shadow evidence (#240 §5)."""
    reasons = []
    n = (metrics.get("by_procedure") or {}).get(procedure.procedure_id, 0)
    if n < min_triggers:
        reasons.append(f"only {n} shadow trigger(s); need {min_triggers}")
    if metrics.get("override_rate", 1.0) > max_override_rate:
        reasons.append(
            f"override rate {metrics.get('override_rate')} exceeds "
            f"{max_override_rate} — the rule fires when it should not")
    if procedure.conflicts_with:
        reasons.append("unresolved conflicts must stay advisory")
    return ValidationResult(ok=not reasons, reasons=reasons)


# ── persistence ───────────────────────────────────────────────────────


def append_procedure(path: str, procedure: Procedure) -> None:
    """Append-only. A version is never rewritten in place."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(procedure.to_dict(), ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        logger.warning("procedural_memory: could not append to %s", path)


def load_procedures(path: str) -> list:
    """Load the latest state of each `procedure_id@version`.

    Later lines for the same key win, so a deprecation appended after an
    activation is what you get — while both remain on disk for audit.
    """
    if not os.path.exists(path):
        return []
    latest: dict = {}
    try:
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                scope = Scope(**(d.pop("scope", {}) or {}))
                ev = [Evidence(**e) for e in (d.pop("evidence", []) or [])]
                p = Procedure(**{**d, "scope": scope, "evidence": ev})
                latest[p.key] = p
    except Exception:  # noqa: BLE001
        return list(latest.values())
    return list(latest.values())


def telemetry(procedures_matched: list, *, task_id: str, context_id: str = "",
              mode: str = SHADOW) -> dict:
    """Privacy-safe counters. Identifiers and numbers, never rule prose.

    ⛔Agent Crew emits these and makes no economic judgement about them.
    """
    return {
        "task_id": task_id,
        "context_id": context_id,
        "procedure_schema_version": PROCEDURE_SCHEMA_VERSION,
        "mode": mode,
        "procedures_matched": len(procedures_matched),
        "procedure_keys": sorted(p.key for p, _ in procedures_matched),
        "procedure_tokens": sum(
            max(1, (len(p.render()) + 3) // 4) for p, _ in procedures_matched),
        "hard_count": sum(1 for p, _ in procedures_matched
                          if p.enforcement == HARD),
        "shadow_count": sum(1 for p, _ in procedures_matched
                            if p.enforcement == SHADOW),
    }
