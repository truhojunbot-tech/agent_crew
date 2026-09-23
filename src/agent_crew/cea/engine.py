"""T1 — the admission engine: ``authorize(intent, caller) -> receipt`` (ADR Π P1–P7, §3, §4, §7).

P1: "exactly one component decides whether work may exist". This module is that
component. Everything it consults is an *input provider* (data, never a verdict,
:mod:`agent_crew.cea.providers`); everything downstream is the *receipt
validator* (:mod:`agent_crew.cea.validator`), which re-checks a receipt and never
re-decides policy.

The nine judgements (§2.3), in the order :meth:`AuthorizationEngine.authorize`
performs them:

=====  ==========================================================================
J9     caller authentication — who is asking (§6.5, P2a)
J1     intent identity — ``intent_hash`` and the live/completed lineage (P4)
J2     authority exists — the snapshot lists a decision in scope (§5.3)
J3     authority still applies — ``source_decision_revs`` (edits invalidate, §1.4)
J4     capability match — the E4 registry, the only matcher (§5.1)
J5     ownership / reuse — an owner that is not the caller needs an approval (§6.2)
J6     runtime state + provider budget — P6 row, Qouta as an input (O9)
J7     review / test contract — the snapshot's ``review_test_matrix`` (O10)
J8     human gate — snapshot predicates and decision records only
=====  ==========================================================================

Two rules shape every path through them:

**P7 — fail closed, and never shadow-ALLOW a missing input.** A provider that
does not exist yet reports *unavailable*; it does not guess. An unavailable input
means ``B′`` cannot be computed, so admission BLOCKs. The ``shadow`` config switch
belongs to the **adapters** — whether they honour the decision — and never to the
engine: the engine always decides truthfully, so a shadow deployment still writes
the receipt that says the input was missing.

**P2a — degraded identity is stated, not assumed away.** Under one uid there is no
credential boundary. With no engine key the receipt is unsigned and both identity
statuses are ``UNVERIFIED`` with ``downgrade_reason``
``SHARED_UID_NO_CREDENTIAL_BOUNDARY``; an identity-dependent decision may then be
``REVIEW`` or ``HUMAN_GATE`` but never ``ALLOW``.

The engine runs in-process today. :class:`EngineConfig.endpoint` selects a
unix-socket client instead (:mod:`agent_crew.cea.service`), so moving to a
``crew-authz`` deployment is a config change, not a rewrite.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from agent_crew.cea._caller_mint import is_authenticated_caller
from agent_crew.cea.intent import (
    IDENTITY_DEPENDENT_WORK_CLASSES, Caller, IdentityStatus, Intent, IntentIdentity,
    InvalidScopeAnchor, Target, WorkClass, canonical_identity)
from agent_crew.cea.providers import (
    CapabilityLookup, PolicySnapshotRef, SignatureStatus)
from agent_crew.cea.runtime_state import RuntimeState, RuntimeStateSnapshot
from agent_crew.cea.schema import canonical_json, validate_receipt
from agent_crew.cea import store as receipt_store

# ═══════════════════════════════════════════════════════════════════════════
# P4 — intent identity
# ═══════════════════════════════════════════════════════════════════════════

def intent_hash(identity: IntentIdentity) -> str:
    """``sha256:<64 hex>`` over exactly the P4 input tuple.

    ``{project, work_class, target{repo, base_ref, scope_anchors[]},
    capability_id | null, authority_decision_ids[]}`` — and nothing else. The
    description, the ``task_id`` and the ``operation_id`` are **not** members:
    rewording work or minting a new opid does not make it new work (P4; E10 4c,
    fixture CX-4c).

    ``scope_anchors`` are **canonicalised** (:func:`~agent_crew.cea.intent.canonical_scope_anchor`),
    then sorted and de-duplicated, as are ``authority_decision_ids``. Sorting
    alone was not enough: ``src/x.py`` and ``src/./x.py`` are one file and used
    to hash differently, which opened two live lineages for it (codex P1 #5). Identity is a set question — the same three files named in a
    different order are the same target — and leaving the order in would give a
    caller a trivial way to mint a "different" intent for identical work, which
    is the very evasion P4 exists to close.
    """
    return _hash_identity(identity, anchors=list(
        canonical_identity(identity).target.scope_anchors))


def _hash_identity(identity: IntentIdentity, *, anchors) -> str:
    body = {
        "project": identity.project,
        "work_class": _work_class_value(identity.work_class),
        "target": {
            "repo": identity.target.repo,
            "base_ref": identity.target.base_ref,
            "scope_anchors": anchors,
        },
        "capability_id": identity.capability_id,
        "authority_decision_ids": sorted(set(identity.authority_decision_ids)),
    }
    digest = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def work_hash(identity: IntentIdentity) -> str:
    """``intent_hash`` with ``authority_decision_ids`` emptied — the *work* itself.

    P4 makes the authority ids part of the intent so a superseding decision
    record yields a new lineage. The cost is that "has this work already
    completed?" can no longer be asked of ``intent_hash`` alone: adding any id
    changes the hash, the completed lineage is not found, and a miss looks
    exactly like work that never ran (codex P1 #2). This hash is what that
    question ranges over.
    """
    return intent_hash(replace(identity, authority_decision_ids=()))


def uncanonical_intent_hash(identity: IntentIdentity) -> str:
    """The hash of an identity whose anchors have **no** canonical form.

    A refusal receipt still needs an ``intent_hash`` — P2 wants the audit row
    even when the answer is "this intent is malformed" — and inventing one would
    be worse than hashing what was actually sent. It can never collide with a
    real lineage: an identity that reaches :func:`intent_hash` has anchors that
    already canonicalised.
    """
    return _hash_identity(identity, anchors=sorted(set(identity.target.scope_anchors)))


def _work_class_value(work_class) -> str:
    return work_class.value if isinstance(work_class, WorkClass) else str(work_class)


# ═══════════════════════════════════════════════════════════════════════════
# J7 floor — the contract the snapshot may tighten and nothing may reduce
# ═══════════════════════════════════════════════════════════════════════════

REVIEW_FLOOR: dict[str, dict[str, bool]] = {
    "implement": {"reviewer": True, "tester": True},
    "fix": {"reviewer": True, "tester": True},
    "merge": {"reviewer": True, "tester": False},
    "review": {"reviewer": False, "tester": False},
    "test": {"reviewer": False, "tester": False},
    "ops": {"reviewer": False, "tester": False},
}
"""The review/test requirement when the snapshot names none for a work class.

⛔This is a **floor**, not a default the caller can argue with. §7.2: a
  ``coordinator_managed`` flag is provenance and "never reduces the contract"
  (fixture CX-4b). The snapshot may require *more* — it may never require less,
  and :func:`_j7_contract` enforces that direction explicitly rather than
  trusting the snapshot to be well-formed.
"""

DEFAULT_ROLE_AGENTS = {"implementer": "claude", "reviewer": "codex", "tester": "gemini"}

_WORK_CLASS_ROLE = {
    "implement": "implementer", "fix": "implementer", "merge": "implementer",
    "ops": "implementer", "review": "reviewer", "test": "tester",
}


# ═══════════════════════════════════════════════════════════════════════════
# config — the service boundary is a switch, not a rewrite
# ═══════════════════════════════════════════════════════════════════════════

OFF = "off"
SHADOW = "shadow"
ENFORCE = "enforce"
TEST = "test"

MODES = (OFF, TEST, SHADOW, ENFORCE)

EMBEDDED_MODES = (OFF, TEST, SHADOW)


def project_mode_env_var(project: str) -> str:
    """The per-project override's environment variable name.

    ``AGENT_CREW_CEA_MODE__<PROJECT>``, project upper-cased with every run of
    non-alphanumeric characters folded to one underscore. One function, because
    the writer (an operator, or `crew setup`) and the reader must agree on the
    spelling and a second copy of this rule would be a silent no-op override.
    """
    key = re.sub(r"[^A-Za-z0-9]+", "_", (project or "").strip()).strip("_").upper()
    return f"AGENT_CREW_CEA_MODE__{key}" if key else "AGENT_CREW_CEA_MODE"


def resolve_mode(env: dict, project: Optional[str] = None) -> str:
    """Rollout config: the engine mode in force for ``project``.

    Precedence: the per-project variable, then the process-wide
    ``AGENT_CREW_CEA_MODE``, then :data:`SHADOW`.

    ⛔Per-project, because rollout is per-project. A single process-wide switch
      forces the whole fleet across the shadow→enforce boundary at once, which
      means the first project that is ready cannot move until the last one is —
      so in practice nobody moves. An unrecognised value falls back to
      :data:`SHADOW` rather than raising: a typo in an operator's environment
      must not be able to take admission down, and shadow is the mode that
      changes nothing while still measuring.

    ⛔The fallback direction is deliberately *not* fail-closed. That asymmetry
      is only sound because this setting decides whether the engine's verdict
      **stops work**, never what the verdict is (P7: the engine has no shadow
      branch). Failing a typo closed would refuse real work over a misspelling.
    """
    for name in (project_mode_env_var(project) if project else None,
                 "AGENT_CREW_CEA_MODE"):
        if not name:
            continue
        raw = (env.get(name) or "").strip().lower()
        if raw:
            return raw if raw in MODES else SHADOW
    return SHADOW
"""The modes in which :meth:`AuthorizationEngine.authorize` may run embedded.

⛔Codex, re-reviewing ``f1aee1d``: an in-process Python construct is not an
  authentication boundary, so the engine must "keep [itself] behind the
  credential-validating service/process boundary (or refuse embedded direct
  authorization) until an actual broker capability boundary exists". This is
  that refusal, and it is what makes the choice a *deployment* decision rather
  than a claim about class privacy.

  ``shadow`` may run embedded because it stops nothing: it measures. ``test``
  may run embedded because it is a test harness and says so in its name. In
  ``enforce`` — the only mode where the engine's verdict withholds work — the
  engine must be reached across the unix socket in :mod:`agent_crew.cea.service`,
  where the credential is checked by a peer that is not the caller. Same-uid
  peers are still untrusted and ``caller_identity_status`` is still UNVERIFIED
  by contract until the O21b broker (see ``src/agent_crew/cea/README.md``); the
  socket is a process boundary, not a verified identity.
"""


@dataclass(frozen=True)
class EngineConfig:
    """How this runtime reaches the engine, and what the adapters do with it.

    ``mode`` is read by the **adapters** to decide whether a non-ALLOW receipt
    stops the work: in ``shadow`` they record it and proceed as they do today; in
    ``test`` and ``enforce`` only PROCEED proceeds. The engine's verdict is
    identical in all three — that is what makes the shadow measurement worth
    anything, and it is why "shadow-ALLOW because an input was missing" cannot
    happen: the engine has no shadow branch to take.

    ``mode`` is read by :meth:`AuthorizationEngine.authorize` for exactly one
    thing, and it is not a verdict: whether this deployment is allowed to
    authorize *embedded* at all (:data:`EMBEDDED_MODES`). ``enforce`` is not,
    because the credential that would make a caller mean anything is checked at
    the socket, not in this interpreter.

    ``test`` is ``enforce`` minus that deployment requirement — enforcement in a
    harness, where there is no socket and no production consequence. ⛔Setting
    ``AGENT_CREW_CEA_MODE=test`` in a real deployment gets enforcement without a
    credential boundary. That is a named, explicit choice rather than a silent
    default, which is the only honest way to offer it.
    """
    mode: str = SHADOW
    endpoint: Optional[str] = None          # unix socket path; None ⇒ in-process
    key_path: Optional[str] = None          # engine signing key (P5, O3)
    issuer: str = "agent_crew.cea.engine"
    role_agents: dict = field(default_factory=lambda: dict(DEFAULT_ROLE_AGENTS))
    default_max_attempts: int = 1
    caller_token_path: Optional[str] = None
    """J9 client side: the file holding *this adapter's own* token.

    An adapter proves who it is by presenting a secret the engine can check; it
    does not get to describe itself in the request body (:mod:`agent_crew.cea.auth`).
    ``None`` means this adapter has no credential, so a remote engine answers 401.
    """

    project: Optional[str] = None
    """Which project's rollout setting produced :attr:`mode` — provenance, so a
    recorded gate answer can be read back against the config that produced it."""

    @classmethod
    def from_env(cls, env: Optional[dict] = None,
                 project: Optional[str] = None) -> "EngineConfig":
        """``project`` selects the per-project rollout override (:func:`resolve_mode`)."""
        e = os.environ if env is None else env
        mode = resolve_mode(e, project)
        return cls(
            mode=mode,
            project=project or None,
            endpoint=(e.get("AGENT_CREW_CEA_ENGINE_ENDPOINT") or "").strip() or None,
            key_path=(e.get("AGENT_CREW_CEA_ENGINE_KEY") or "").strip() or None,
            issuer=(e.get("AGENT_CREW_CEA_ISSUER") or "").strip() or cls.issuer,
            caller_token_path=(e.get("AGENT_CREW_CEA_ADAPTER_TOKEN_FILE") or "").strip() or None,
        )

    @property
    def enforcing(self) -> bool:
        """Does a non-PROCEED answer stop the work? ``test`` enforces like
        ``enforce``; they differ only in where the engine is allowed to run.
        ``off`` and ``shadow`` both proceed — they differ in whether the answer
        is computed and recorded at all."""
        return self.mode in (ENFORCE, TEST)

    @property
    def recording(self) -> bool:
        """Is a gate answer worth computing here? ``off`` is the rollout escape
        hatch: no verdict is produced, so nothing is recorded and nothing is
        measured. It exists so "turn the engine off for this project" is a
        config change rather than a deploy, and it is never the default."""
        return self.mode != OFF

    @property
    def embedded_authorization_permitted(self) -> bool:
        """May ``authorize()`` run in this process? See :data:`EMBEDDED_MODES`."""
        return self.mode in EMBEDDED_MODES


# ═══════════════════════════════════════════════════════════════════════════
# outcome
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Authorization:
    """What an adapter gets back. The receipt is always present — including for
    BLOCK: P2 requires the audit row even when no task row is written."""
    receipt: dict
    http_status: int
    code: str
    reused: bool = False
    existing_receipt_id: Optional[str] = None

    @property
    def decision(self) -> str:
        return self.receipt["decision"]

    @property
    def allowed(self) -> bool:
        return self.decision == "ALLOW"

    @property
    def receipt_id(self) -> str:
        return self.receipt["receipt_id"]


class EngineError(Exception):
    """The engine could not produce a receipt at all (never a policy outcome)."""


class UnauthenticatedCaller(EngineError):
    """J9 refused before any input was read: 401, and **no receipt** (CXC-4).

    A subclass of :class:`EngineError` so every existing adapter that already
    fails closed on an engine error keeps doing so; the distinct type is what
    lets an adapter answer 401 rather than 500.
    """




# ═══════════════════════════════════════════════════════════════════════════
# default providers — "does not exist yet" is reported, never guessed
# ═══════════════════════════════════════════════════════════════════════════

class UnavailableCapabilityRegistry:
    """No E4 registry wired. Reports unavailable so P7 decides, not this class."""

    def lookup(self, intent: Intent) -> CapabilityLookup:
        return CapabilityLookup(registry=None, available=False)


class UnavailablePolicySnapshot:
    def current(self, intent: Optional[Intent] = None) -> PolicySnapshotRef:
        return PolicySnapshotRef(generation=0, hash="", produced_at=None, decisions=(),
                                 available=False)


class UnavailableRuntimeState:
    def current(self) -> RuntimeStateSnapshot:
        return RuntimeStateSnapshot(state=RuntimeState.STOPPED, epoch=0, read_failed=True)


class UnavailableBudget:
    def budget(self, provider: str):
        from agent_crew.cea.receipt import BudgetClass, ProviderBudget
        return ProviderBudget(provider=provider, state=BudgetClass.EXHAUSTED, observed_at=None)


class SnapshotHumanGate:
    """J8 — the gate is whatever the snapshot's predicates say, and nothing else."""

    def state(self, intent: Intent, snapshot: PolicySnapshotRef):
        from agent_crew.cea.receipt import HumanGate, HumanGateState
        for pred in snapshot.human_gate_predicates or ():
            if not isinstance(pred, dict):
                continue
            if not _predicate_matches(pred, intent):
                continue
            state = str(pred.get("state") or "PENDING")
            if state == "GRANTED" and pred.get("decision_id"):
                return HumanGate(HumanGateState.GRANTED, str(pred["decision_id"]))
            if state == "DENIED":
                return HumanGate(HumanGateState.DENIED)
            return HumanGate(HumanGateState.PENDING)
        return HumanGate(HumanGateState.NOT_REQUIRED)


def _predicate_matches(pred: dict, intent: Intent) -> bool:
    for key, value in (("project", intent.identity.project),
                       ("work_class", _work_class_value(intent.identity.work_class)),
                       ("capability_id", intent.identity.capability_id),
                       ("repo", intent.identity.target.repo)):
        if key in pred and pred[key] not in (None, "*") and pred[key] != value:
            return False
    return True


# ═══════════════════════════════════════════════════════════════════════════
# the engine
# ═══════════════════════════════════════════════════════════════════════════

class AuthorizationEngine:
    """T1. One instance per runtime; :meth:`authorize` is the only entry point.

    Every stored side effect happens on the connection the caller hands in, so an
    adapter can run admission inside the same ``BEGIN IMMEDIATE`` that writes the
    task row: the receipt and the ``QUEUED`` row commit together or not at all.
    """

    def __init__(self, *, config: Optional[EngineConfig] = None,
                 capabilities=None, snapshots=None, runtime=None, budgets=None,
                 gates=None, clock=None):
        self.config = config or EngineConfig.from_env()
        self.capabilities = capabilities or UnavailableCapabilityRegistry()
        self.snapshots = snapshots or UnavailablePolicySnapshot()
        self.runtime = runtime or UnavailableRuntimeState()
        self.budgets = budgets or UnavailableBudget()
        self.gates = gates or SnapshotHumanGate()
        self._clock = clock or time.time
        self._key = _load_key(self.config.key_path)

    # ── caller + identity preconditions, shared by both entry points ────

    def _require_authenticated(self, caller: Caller) -> None:
        """J9 — who is asking. Raise before anything is read: an unauthenticated
        caller does not get to learn the policy state, and it does not get an
        audit row in its chosen name either.

        ⛔A Caller is only ever a *result of authentication*
          (:mod:`agent_crew.cea.auth`). Two earlier versions of this test were
          not authentication:
            `caller is None` — the socket decoder built a Caller from request
              JSON, so `Caller(principal="attacker", ...)` walked through and an
              OPS intent under it was ALLOWed (codex P1 #1);
            `credential_kind in (...)` — a *string the caller chose*, so
              `Caller(principal="attacker", provenance=DIRECT,
              identity_status=VERIFIED, credential_kind="broker_registered")`
              walked through the same public entry point and reached the receipt
              as caller_identity_status=VERIFIED, although agent_crew.cea.auth
              has no broker producer at all (codex P1 #4, re-review of cb01d49).
          The test is now *provenance of the object itself*: was it produced by
          an authenticator, which happens only after a presented credential
          matched. A forgery cannot set that by copying fields.

          ⛔It is still not an authentication boundary and is not claimed as one
            (codex, re-review of f1aee1d): in-process code can import
            `_caller_mint.mint_caller`, or add an `object.__new__` instance to
            the registry via `is_authenticated_caller.__closure__`. What makes
            that worthless is that no judgement differs by principal while
            identity is UNVERIFIED — and that under `enforce` the public entry
            point decides nothing at all.
        """
        if caller is None:
            raise UnauthenticatedCaller(
                "authorize() requires an authenticated Caller; adapters authenticate first (§7.1)")
        if not is_authenticated_caller(caller):
            raise UnauthenticatedCaller(
                f"caller {getattr(caller, 'principal', None)!r} was not minted by an "
                f"authenticator; a Caller comes from agent_crew.cea.auth after a credential "
                f"matched, never from a request body or a constructor (J9, P2a)")

    def _canonicalize(self, conn: sqlite3.Connection, intent: Intent, caller: Caller):
        """P4/§5.2: one spelling per target, fixed *before* the hash and before
        the E4 registry sees the intent — so identity and matching can never
        disagree about which file was named.

        Returns ``(intent, intent_hash)``, or an :class:`Authorization` refusal
        when the anchors do not canonicalize.
        """
        try:
            intent = replace(intent, identity=canonical_identity(intent.identity))
        except InvalidScopeAnchor as exc:
            return Authorization(
                receipt=self._refusal(intent, caller, uncanonical_intent_hash(intent.identity),
                                      "INVALID_SCOPE_ANCHOR", str(exc), conn=conn),
                http_status=400, code="INVALID_SCOPE_ANCHOR")
        return intent, intent_hash(intent.identity)

    # ── public API ──────────────────────────────────────────────────────

    def authorize(self, conn: sqlite3.Connection, intent: Intent, caller: Caller, *,
                  retry: bool = False) -> Authorization:
        """Decide whether this intent may exist, and record the receipt.

        Returns an :class:`Authorization` whose ``http_status`` is the adapter's
        answer: ``201`` newly admitted, ``200`` the same receipt returned for an
        idempotent replay, ``409`` ``DUPLICATE_INTENT``, ``403`` blocked/held,
        ``401`` no caller credential.

        ⛔In ``mode=enforce`` this method is **unconditionally fail-closed**. It
          is the entry point any in-process caller can reach, so under P7 it can
          never be the one that enforces. The only enforcing path is
          :meth:`_authorize_authenticated`, which
          :class:`~agent_crew.cea.service.EngineService` calls after the
          credential it was handed matched a token by ``hmac.compare_digest`` —
          in a process the caller does not run in.
        """
        if self.config.mode == ENFORCE:
            return self._enforce_boundary_refusal(conn, intent, caller)
        return self._authorize_authenticated(conn, intent, caller, retry=retry)

    def _enforce_boundary_refusal(self, conn: sqlite3.Connection, intent: Intent,
                                  caller: Caller) -> Authorization:
        """P7/P2a: ``enforce`` reached without a credential boundary, so nothing
        is decided and the refusal is recorded as an unavailable input.

        Previously this depended on ``self._credential_boundary is None`` — a
        public mutable flag any caller could set (``eng.attach_credential_boundary
        ('/not-a-socket')``), after which a forged mint reached ALLOW with
        ``caller_identity=attacker`` and no socket in existence at all (codex
        review-sev0-cea-lineage-s2a-fix-r3-x P1). A declaration is not proof of
        process topology; being a different method than the one the boundary
        calls is.
        """
        receipt_store.ensure_schema(conn)
        self._require_authenticated(caller)
        prepared = self._canonicalize(conn, intent, caller)
        if isinstance(prepared, Authorization):
            return prepared
        intent, ih = prepared
        return Authorization(
            receipt=self._refusal(
                intent, caller, ih, "CREDENTIAL_BOUNDARY_UNAVAILABLE",
                "P7/P2a: mode=enforce refuses embedded in-process authorization. The caller "
                "credential boundary is the unix-socket service (AGENT_CREW_CEA_ENGINE_ENDPOINT), "
                "not this interpreter; an in-process caller object is not authentication. Use "
                "mode=shadow to measure, mode=test in a harness, or deploy crew-authz",
                conn=conn, unavailable=("caller_credential_boundary",)),
            http_status=403, code="CREDENTIAL_BOUNDARY_UNAVAILABLE")

    # ── boundary-internal API ───────────────────────────────────────────
    # ⛔Reachable only from the credential boundary. It is a private name, not a
    #   permission check: in-process code can still import and call it, exactly
    #   as it can import `_caller_mint.mint_caller`. That residue is the P2a
    #   same-uid limitation and is documented in cea/README.md as such — what
    #   changed is that no *public* enforce path exists to walk through any more.

    def _authorize_authenticated(self, conn: sqlite3.Connection, intent: Intent,
                                 caller: Caller, *, retry: bool = False) -> Authorization:
        """The real decision. Called by :class:`EngineService` once the presented
        credential matched, and directly by the non-enforcing modes."""
        receipt_store.ensure_schema(conn)

        # J9 — who is asking. 401 before anything else is read: an
        # unauthenticated caller does not get to learn the policy state, and it
        # does not get an audit row in its chosen name either.
        #
        # ⛔A Caller is only ever a *result of authentication*
        #   (:mod:`agent_crew.cea.auth`). Two earlier versions of this test were
        #   not authentication:
        #     `caller is None` — the socket decoder built a Caller from request
        #       JSON, so `Caller(principal="attacker", ...)` walked through and
        #       an OPS intent under it was ALLOWed (codex P1 #1);
        #     `credential_kind in (...)` — a *string the caller chose*, so
        #       `Caller(principal="attacker", provenance=DIRECT,
        #       identity_status=VERIFIED, credential_kind="broker_registered")`
        #       walked through the same public entry point and reached the
        #       receipt as caller_identity_status=VERIFIED, although
        #       agent_crew.cea.auth has no broker producer at all (codex P1 #4,
        #       re-review of cb01d49).
        #   The test is now *provenance of the object itself*: was it produced
        #   by an authenticator, which happens only after a presented credential
        #   matched. A forgery cannot set that by copying fields.
        #
        #   ⛔It is still not an authentication boundary and is not claimed as
        #     one (codex, re-review of f1aee1d): in-process code can import
        #     `_caller_mint.mint_caller`, or add an `object.__new__` instance to
        #     the registry via `is_authenticated_caller.__closure__`. Both
        #     produce a caller this test accepts. What makes that worthless is
        #     below — no judgement differs by principal while identity is
        #     UNVERIFIED — and the ENFORCE boundary check that follows.
        self._require_authenticated(caller)

        prepared = self._canonicalize(conn, intent, caller)
        if isinstance(prepared, Authorization):
            return prepared
        intent, ih = prepared

        # J1 — lineage. Answered before the expensive inputs so a replay costs a
        # single indexed read, which is what makes idempotency cheap enough to be
        # on every path.
        lineage = receipt_store.lineage_for_intent(conn, ih)
        if lineage is not None:
            decided = self._existing_lineage(conn, intent, caller, ih, lineage, retry=retry)
            if decided is not None:
                return decided

        # J2–J8
        executor = self._executor_binding(intent)
        snapshot, registry, runtime, budget, gate, raised = self._read_inputs(intent, executor)

        # P4's completed-work exception, asked of the work and not of the hash.
        completed = self._completed_work_refusal(conn, intent, caller, ih, snapshot,
                                                 unavailable=bool(raised))
        if completed is not None:
            return completed

        unavailable = self._unavailable_inputs(snapshot, registry, runtime) + raised
        reviewer, tester = self._j7_contract(intent, snapshot, executor)
        reuse = self._j5_reuse(intent, registry)

        receipt = self._build(
            intent=intent, caller=caller, intent_hash_value=ih, snapshot=snapshot,
            registry=registry, runtime=runtime, budget=budget, gate=gate,
            executor=executor, reviewer=reviewer, tester=tester, reuse=reuse,
            unavailable=unavailable)

        return self._record(conn, receipt, ih, new_lineage=True, wh=work_hash(intent.identity))

    def mint_dispatch_nonce(self, conn: sqlite3.Connection, receipt: dict) -> tuple[dict, str]:
        """P2 dispatch: a fresh single-use nonce bound to ``(receipt_id, attempt)``.

        The ``UNIQUE(receipt_id, attempt)`` constraint in the store is what makes
        this safe against two dispatchers racing: the loser's INSERT raises
        rather than minting a second live nonce for one attempt.
        """
        nonce = secrets.token_hex(16)
        issued = _rfc3339(self._clock())
        receipt_store.mint_nonce(conn, receipt["receipt_id"], int(receipt["attempt"]), nonce, issued)
        nonces = list(receipt.get("dispatch_nonces") or [])
        nonces.append({"nonce": nonce, "attempt": int(receipt["attempt"]),
                       "issued_at": issued, "used_at": None})
        updated = self._resign(dict(receipt, dispatch_nonces=nonces))
        receipt_store.record_receipt(conn, updated, recorded_by="engine", note="dispatch nonce minted")
        return updated, nonce

    def transition(self, conn: sqlite3.Connection, receipt_id: str, state: str, *,
                   note: Optional[str] = None, mutate: Optional[dict] = None) -> dict:
        """Append a lifecycle row and keep the P4 lineage claim in step with it.

        ⛔Goes through :func:`receipt_store.append_lifecycle`, which is where the
          §3 graph and the terminal-state rule live. Calling ``record_receipt``
          here instead put the guard on a road nobody drove down: the store-level
          test passed while ``authorize → transition(CONSUMED) → transition(RUNNING)``
          still succeeded through the engine and ``current_receipt`` answered
          RUNNING — a consumed receipt walked back out of a terminal state by the
          only route the engine actually offers (codex re-review of 4f79ce4, P1 #2).

        The lineage claim is updated **after** the append, so a refused transition
        leaves the lineage describing the state the receipt is really in.

        ``mutate`` carries the fields the transition itself legitimately changes
        and is re-signed with the rest of the body — §8's re-admission bumps
        ``attempt`` in the same row that records ``RE-QUEUED``, because an
        attempt count written by a second, unsigned write would be a number the
        signature does not cover.
        """
        try:
            updated = receipt_store.append_lifecycle(
                conn, receipt_id, state, recorded_by="engine", note=note, mutate=mutate,
                sign=self._resign)
        except receipt_store.ReceiptStoreError as exc:
            raise EngineError(str(exc)) from exc
        receipt_store.set_lineage_state(conn, updated["intent_hash"], receipt_id, updated["state"])
        return updated

    def current_binding(self, receipt: dict) -> tuple[Optional[dict], tuple[str, ...]]:
        """``(B′, unavailable_inputs)`` for a receipt already in flight (P3, P7).

        The post-admission call sites (claim, dispatch, execute-start, result)
        have a receipt, not an intent, and B′ has to be computed over the scope
        admission used — so it is read back from ``provenance.intent_identity``
        and the providers are asked again. Any input that did not answer makes B′
        ``None`` with the names attached: the validator then fails closed at
        admission and *holds* work already authorised, which is P7's asymmetry
        and not something an adapter gets to reinterpret.
        """
        intent = intent_from_receipt(receipt)
        if intent is None:
            return None, ("intent_identity",)
        executor = receipt.get("executor_binding") or self._executor_binding(intent)
        snapshot, registry, runtime, budget, gate, raised = self._read_inputs(intent, executor)
        unavailable = self._unavailable_inputs(snapshot, registry, runtime) + raised
        if unavailable:
            return None, unavailable
        return self._binding(snapshot, registry, runtime, budget, gate, _matched(registry)), ()

    # ── J1: an existing lineage ─────────────────────────────────────────

    def _existing_lineage(self, conn, intent: Intent, caller: Caller, ih: str,
                          lineage: dict, *, retry: bool) -> Optional[Authorization]:
        """P4's three answers for an intent that already has a lineage.

        Returns ``None`` only when the lineage is free (SUPERSEDED/REVOKED) and
        admission should proceed normally.
        """
        state = lineage["state"]
        prior = receipt_store.current_receipt(conn, lineage["receipt_id"])
        if prior is None or state in ("SUPERSEDED", "REVOKED"):
            return None

        if state == "CONSUMED":
            # P4: completed work is not re-admitted. The exception is a
            # superseding decision record — and that needs no special case here,
            # because `authority_decision_ids` is an intent_hash input, so a
            # newer decision produces a different hash and lands as a new lineage.
            return Authorization(
                receipt=self._refusal(intent, caller, ih, "ALREADY_COMPLETED",
                                     f"this intent completed as receipt {lineage['receipt_id']}; "
                                     f"re-admission requires a superseding decision record (P4)",
                                     conn=conn),
                http_status=409, code="ALREADY_COMPLETED",
                existing_receipt_id=lineage["receipt_id"])

        if retry:
            # P4 retry rule: the same receipt is reused iff B is unchanged and
            # there is an attempt left. Anything else is a re-admission, which
            # means the engine runs again — so we free the lineage and fall
            # through rather than inventing a half-valid receipt here.
            if int(prior["attempt"]) < int(prior["max_attempts"]) and not self._binding_drifted(intent, prior):
                bumped = self._resign(dict(prior, attempt=int(prior["attempt"]) + 1))
                receipt_store.record_receipt(conn, bumped, recorded_by="engine",
                                             note="retry: B unchanged, attempt incremented (P4)")
                return Authorization(receipt=bumped, http_status=200, code="RETRY_SAME_RECEIPT",
                                     reused=True, existing_receipt_id=prior["receipt_id"])
            self._supersede(conn, prior, "retry exhausted the attempt budget or B drifted (P4)")
            return None

        key = intent.idempotency_key
        if key is not None and key == prior.get("idempotency_key"):
            # Same intent, same key: the caller is retrying the *request*, not
            # asking for more work. It gets the receipt it already has.
            return Authorization(receipt=prior, http_status=200, code="IDEMPOTENT_REPLAY",
                                 reused=True, existing_receipt_id=prior["receipt_id"])

        return Authorization(
            receipt=self._refusal(intent, caller, ih, "DUPLICATE_INTENT",
                                  f"an identical intent is live as receipt {lineage['receipt_id']} "
                                  f"(state {state}); a new task_id or description does not make it "
                                  f"new work (P4)", conn=conn),
            http_status=409, code="DUPLICATE_INTENT",
            existing_receipt_id=lineage["receipt_id"])

    def _completed_work_refusal(self, conn, intent: Intent, caller: Caller, ih: str,
                                snapshot, *, unavailable: bool) -> Optional[Authorization]:
        """P4: completed work is re-admitted only by a **verified superseding record**.

        ⛔The old code left this to ``intent_hash`` alone: "a newer decision
          produces a different hash and lands as a new lineage". So did an older
          one, and so did a made-up one. After an OPS lineage completed under
          T0-1234, the caller-supplied tuple ``(T0-1234, ATTACKER-ID)`` changed
          the hash, missed ALREADY_COMPLETED entirely and was ALLOWed — while the
          signed snapshot still contained only T0-1234 (codex P1 #2). A hash
          input a caller controls is a nonce, not an authorisation.

        The exception is granted only when the *signed snapshot* carries a
        decision record that (a) is one of the ids this request asks to act
        under, and (b) explicitly names the completed run's authority in its
        ``supersedes``. Everything else — including an unreadable snapshot — is
        the refusal, because a supersession we cannot verify is not one.
        """
        wh = work_hash(intent.identity)
        prior = receipt_store.completed_lineage_for_work(conn, wh, exclude_intent_hash=ih)
        if prior is None:
            return None
        prior_receipt = receipt_store.current_receipt(conn, prior["receipt_id"])
        prior_ids = tuple((prior_receipt or {}).get("authority_source", {}).get("decision_ids") or ())
        if not unavailable and self._supersession_record(intent, snapshot, prior_ids) is not None:
            return None
        return Authorization(
            receipt=self._refusal(
                intent, caller, ih, "ALREADY_COMPLETED",
                f"this work completed as receipt {prior['receipt_id']} under authority "
                f"{list(prior_ids)}; re-admission requires a decision record in the signed "
                f"snapshot that explicitly supersedes it, not a caller-supplied id (P4)",
                conn=conn),
            http_status=409, code="ALREADY_COMPLETED",
            existing_receipt_id=prior["receipt_id"])

    @staticmethod
    def _supersession_record(intent: Intent, snapshot, prior_ids):
        """The snapshot record that supersedes ``prior_ids``, or ``None``."""
        if not prior_ids or snapshot is None or not getattr(snapshot, "available", False):
            return None
        if snapshot.signature is not SignatureStatus.VALID:
            return None
        requested = set(intent.identity.authority_decision_ids)
        wanted = set(prior_ids)
        for rev in (snapshot.in_scope or snapshot.decisions or ()):
            if rev.decision_id in requested and wanted.issubset(set(rev.supersedes or ())):
                return rev
        return None

    def _binding_drifted(self, intent: Intent, prior: dict) -> bool:
        """Is B′ different from the B this receipt was issued under?"""
        from agent_crew.cea.validator import _drifted_fields
        executor = prior.get("executor_binding") or self._executor_binding(intent)
        snapshot, registry, runtime, budget, gate, raised = self._read_inputs(intent, executor)
        if raised:
            # An input we could not read is not "unchanged". Fail closed: treat
            # it as drift, which supersedes the receipt and re-runs admission —
            # where P7 blocks on the same unavailable input, with a receipt.
            return True
        now = self._binding(snapshot, registry, runtime, budget, gate,
                            _matched(registry), binding_shape=True)
        return bool(_drifted_fields(prior.get("binding") or {}, now))

    def _supersede(self, conn, prior: dict, why: str) -> None:
        superseded = self._resign(dict(prior, state="SUPERSEDED"))
        receipt_store.record_receipt(conn, superseded, recorded_by="engine", note=why)
        receipt_store.release_lineage(conn, prior["intent_hash"], prior["receipt_id"])

    # ── J2–J8 helpers ───────────────────────────────────────────────────

    def _read_inputs(self, intent: Intent, executor):
        """Read every J2–J8 input, fail-closed, in one place.

        ⛔A provider that raises is an **unavailable input**, not an exception
          that escapes into the adapter. An escaping error means no receipt at
          all: no audit row, and a 500 that each of the twelve ingresses gets to
          interpret for itself. P7 says the engine decides truthfully when an
          input does not answer, and "the provider threw" is the loudest way an
          input can fail to answer (codex P1 #3, second half).
        """
        raised: list[str] = []

        def read(name, fn, fallback):
            try:
                return fn()
            except Exception:                    # noqa: BLE001 — every failure is one fact: no answer
                raised.append(name)
                return fallback

        snapshot = read("policy_snapshot", lambda: self.snapshots.current(intent),
                        UnavailablePolicySnapshot().current(intent))
        registry = read("capability_registry", lambda: self.capabilities.lookup(intent),
                        UnavailableCapabilityRegistry().lookup(intent))
        runtime = read("runtime_state", lambda: self.runtime.current(),
                       UnavailableRuntimeState().current())
        budget = read("provider_budget", lambda: self.budgets.budget(executor or "unknown"),
                      UnavailableBudget().budget(executor or "unknown"))
        gate = read("human_gate", lambda: self.gates.state(intent, snapshot),
                    _denied_gate())
        return snapshot, registry, runtime, budget, gate, tuple(raised)

    def _unavailable_inputs(self, snapshot, registry, runtime) -> tuple[str, ...]:
        """P7: which inputs did not answer. Named, so the receipt says *what* was missing."""
        missing: list[str] = []
        if not snapshot.available:
            missing.append("policy_snapshot")
        elif snapshot.signature is not SignatureStatus.VALID:
            # ⛔Only VALID is usable. Rejecting INVALID alone let UNSIGNED and
            #   UNKEYED through, and an in-memory reproduction with an otherwise
            #   valid UNSIGNED snapshot produced ALLOW for OPS (codex P1 #3).
            #   §3/P5 require the engine to *verify* the snapshot before use;
            #   "nobody signed it" and "the signature is a sha256[:16] prefix
            #   that is not integrity" (E10 4e) are both failures to verify, and
            #   an unverified authority record is not authority.
            missing.append(f"policy_snapshot_signature:{_signature_name(snapshot.signature)}")
        if not registry.available:
            missing.append("capability_registry")
        elif registry.stale:
            missing.append("capability_registry_stale")
        if runtime.read_failed:
            missing.append("runtime_state")
        return tuple(missing)

    def _j7_contract(self, intent: Intent, snapshot: PolicySnapshotRef,
                     executor: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        """J7 — who must review and who must test, from the snapshot over the floor.

        The snapshot is consulted for *more*, never for less: ``max`` over the
        floor is the whole rule. That is what makes ``risk_tier.py`` removable as
        a decision (§11.1 row 13) without loosening anything, and what makes
        ``coordinator_managed`` incapable of reducing the contract (§7.2).
        """
        wc = _work_class_value(intent.identity.work_class)
        floor = REVIEW_FLOOR.get(wc, {"reviewer": True, "tester": True})
        row = (snapshot.review_test_matrix or {}).get(wc) or {}
        need_reviewer = bool(floor["reviewer"]) or bool(row.get("reviewer"))
        need_tester = bool(floor["tester"]) or bool(row.get("tester"))
        agents = self.config.role_agents or DEFAULT_ROLE_AGENTS
        reviewer = agents.get("reviewer") if need_reviewer else None
        tester = agents.get("tester") if need_tester else None
        # §3: "must differ from executor_binding". An executor that is also the
        # named reviewer is not independence, it is a label.
        if reviewer is not None and reviewer == executor:
            reviewer = next((a for r, a in agents.items()
                             if r != "implementer" and a != executor), None)
        return reviewer, tester

    def _j5_reuse(self, intent: Intent, registry: CapabilityLookup):
        """J4/J5 — the registry matched something someone else owns (§6.2, CX-4i).

        Reuse is only ever *recorded* here. Approval is an owner act with a
        verified identity; under P2a nothing in this process can verify one, so
        ``approver_identity_verified`` is False and the decision path treats the
        receipt as identity-dependent — which is exactly why it cannot be ALLOW.

        ⛔There is deliberately **no caller argument**. This test used to read
          ``owner != caller.principal``, so "am I the owner?" was answered by the
          name the caller arrived under — and under an UNVERIFIED identity that
          name is a claim, not a fact. The consequence was a real principal-
          dependent difference: a caller that named itself ``quota-core`` got
          EXTEND (REVIEW, once a reviewer is named) where everyone else got
          REUSE (HUMAN_GATE, OWNER_CONFLICT). A forged or registry-poisoned
          Caller could therefore *downgrade an owner decision to a review* just
          by choosing a string (codex P1, re-review of f1aee1d).

          The ownership question is now answered only from intent scope —
          ``identity.project``, which is part of ``intent_hash`` and is scope,
          not an identity claim. Every principal gets the same answer for the
          same intent, which is what P2a requires while identity is UNVERIFIED.
          When the O21b broker can verify an owner, *that* is what restores the
          distinction — a verified approval recorded in ``approved_by``.
        """
        from agent_crew.cea.receipt import Reuse, ReuseDecision
        matches = tuple(registry.matches) + tuple(registry.anchor_matches)
        if not matches:
            return Reuse(ReuseDecision.NEW) if registry.available else None
        owner = matches[0].owner
        if owner and owner != intent.identity.project:
            return Reuse(ReuseDecision.REUSE, approved_by=None, approver_identity_verified=False)
        return Reuse(ReuseDecision.EXTEND, approved_by=None, approver_identity_verified=False)

    def _executor_binding(self, intent: Intent) -> Optional[str]:
        agents = self.config.role_agents or DEFAULT_ROLE_AGENTS
        role = _WORK_CLASS_ROLE.get(_work_class_value(intent.identity.work_class), "implementer")
        return agents.get(role)

    # ── receipt construction ────────────────────────────────────────────

    def _binding(self, snapshot, registry, runtime, budget, gate, matched,
                 *, binding_shape: bool = True) -> dict:
        """B (P3). ``budget_class`` has no UNVERIFIED member in the frozen schema,
        so an unobservable budget is recorded as ``EXHAUSTED``: the fail-closed
        value, with the honest ``UNVERIFIED`` kept on the top-level
        ``provider_budget`` where the schema does allow it."""
        bc = getattr(budget.state, "value", budget.state)
        if bc not in ("OK", "CONSTRAINED", "EXHAUSTED"):
            bc = "EXHAUSTED"
        return {
            "policy_generation": int(snapshot.generation or 0),
            "policy_hash": snapshot.hash or "unavailable",
            "source_decision_revs": [{"decision_id": r.decision_id, "body_hash": r.body_hash}
                                     for r in (snapshot.in_scope or snapshot.decisions or ())],
            "capability_registry": _registry_ref(registry),
            "matched_capability": ({"id": matched.id, "owner": matched.owner} if matched else None),
            "runtime_state": getattr(runtime.state, "value", runtime.state),
            "runtime_state_epoch": int(runtime.epoch or 0),
            "human_gate_state": _gate_json(gate),
            "budget_class": bc,
        }

    def _build(self, *, intent, caller, intent_hash_value, snapshot, registry, runtime,
               budget, gate, executor, reviewer, tester, reuse, unavailable) -> dict:
        matched = _matched(registry)
        binding = self._binding(snapshot, registry, runtime, budget, gate, matched)

        # P2a. ⛔The engine's *receipt-signing* key has nothing to say about who
        #   the caller is. It said so before this fix: with any key configured,
        #   `caller.identity_status` was copied through from the request, so a
        #   keyed engine handed a caller-created VERIFIED Caller a receipt
        #   reading `caller_identity_status: VERIFIED` (codex P1 #4). Signing
        #   proves the engine wrote the receipt. It does not authenticate a
        #   shared-uid caller, and conflating the two is how a receipt starts
        #   asserting an identity nobody checked.
        #
        #   VERIFIED is reserved for the O21b broker's spawn/registration path,
        #   which does not exist yet. `_broker_verified(caller)` used to read
        #   `caller.credential_kind == "broker_registered" and
        #   caller.identity_status is VERIFIED` — both caller-set fields, so the
        #   promoter was reachable from the public entry point by anyone willing
        #   to name the kind (codex P1 #4 r2). It is gone. Both statuses are
        #   unconditional here, and the authenticator derives identity_status on
        #   the other side of the boundary (_caller_mint.mint_caller), so there is no
        #   input anywhere that yields VERIFIED in this build.
        caller_status = IdentityStatus.UNVERIFIED
        executor_status = IdentityStatus.UNVERIFIED
        degraded = (caller_status is not IdentityStatus.VERIFIED
                    or executor_status is not IdentityStatus.VERIFIED)

        decision, code, text = self._decide(
            intent=intent, snapshot=snapshot, registry=registry, runtime=runtime,
            gate=gate, binding=binding, unavailable=unavailable, reuse=reuse,
            reviewer=reviewer, degraded=degraded)

        receipt = {
            "receipt_id": str(uuid.uuid4()),
            "issued_at": _rfc3339(self._clock()),
            "issuer": self.config.issuer,
            "task_id": intent.task_id,
            "intent_hash": intent_hash_value,
            "parent_receipt_id": intent.parent_receipt_id,
            "project": intent.identity.project,
            "authority_source": {
                "decision_ids": sorted(set(intent.identity.authority_decision_ids)),
                "tier": snapshot.tier,
            },
            "policy_generation": binding["policy_generation"],
            "policy_hash": binding["policy_hash"],
            "source_decision_revs": binding["source_decision_revs"],
            "capability_registry": binding["capability_registry"],
            "matched_capability": ({"id": matched.id, "owner": matched.owner,
                                    "repo": matched.repo} if matched else None),
            "reuse": (None if reuse is None else
                      {"decision": getattr(reuse.decision, "value", reuse.decision),
                       "approved_by": reuse.approved_by,
                       "approver_identity_verified": bool(reuse.approver_identity_verified)}),
            "runtime_state": binding["runtime_state"],
            "provider_budget": {
                "provider": budget.provider,
                "state": getattr(budget.state, "value", budget.state),
                "observed_at": (_rfc3339(budget.observed_at) if budget.observed_at else None),
            },
            "required_reviewer": reviewer,
            "required_tester": tester,
            "human_gate_state": binding["human_gate_state"],
            "caller_identity": caller.principal,
            "caller_provenance": getattr(caller.provenance, "value", caller.provenance),
            "executor_binding": executor,
            "executor_binding_status": executor_status.value,
            "caller_identity_status": caller_status.value,
            "downgrade_reason": ("SHARED_UID_NO_CREDENTIAL_BOUNDARY" if degraded else None),
            "decision": decision,
            "reason": {"code": code, "text": text},
            "signature": {"alg": None, "key_id": None, "value": None, "status": "UNVERIFIED"},
            "state": "ISSUED",
            "binding": binding,
            "idempotency_key": intent.idempotency_key or intent_hash_value,
            "attempt": 1,
            "max_attempts": max(1, int(self.config.default_max_attempts)),
            "dispatch_nonces": [],
            "supersedes": [],
            "provenance": _provenance(intent, snapshot, unavailable),
        }
        return self._resign(receipt)

    def _decide(self, *, intent, snapshot, registry, runtime, gate, binding,
                unavailable, reuse, reviewer, degraded) -> tuple[str, str, str]:
        """The verdict, in the order the ADR fixes the precedence.

        P7 first: an input that did not answer outranks every other judgement,
        because a decision made without it is not a decision, it is a guess.
        """
        if unavailable:
            return ("BLOCK", "INPUTS_UNAVAILABLE",
                    f"P7: {', '.join(unavailable)} did not answer; admission fails closed and "
                    f"never shadow-ALLOWs a missing input")

        gate_state = _gate_json(gate)
        gate_name = gate_state["state"] if isinstance(gate_state, dict) else gate_state
        if gate_name == "DENIED":
            return ("BLOCK", "HUMAN_GATE_DENIED", "J8: the owner denied this work")
        if gate_name == "PENDING":
            return ("HUMAN_GATE", "HUMAN_GATE_PENDING",
                    "J8: a snapshot predicate requires an owner decision; nothing runs until it exists")

        rs = binding["runtime_state"]
        if rs != "ACTIVE":
            return ("BLOCK", "RUNTIME_STATE_FORBIDS",
                    f"P6: runtime state is {rs}; enqueue is refused in every non-ACTIVE state")

        authorised = {rev.decision_id for rev in (snapshot.in_scope or snapshot.decisions or ())}
        if not authorised:
            return ("BLOCK", "NO_AUTHORITY",
                    "J2: the policy snapshot lists no decision in scope for this intent; "
                    "free text in a task description is not authority (§1.4)")

        # ⛔J2 never checked that the ids the request claims to act under are ids
        #   the snapshot actually authorises for this scope; it only checked that
        #   *some* decision was in scope. So `authority_decision_ids` was an
        #   unvalidated caller string that nonetheless entered `intent_hash`
        #   (codex P1 #2). An id the snapshot does not carry is not authority —
        #   it is free text with a ticket-shaped name (§1.4).
        unknown = sorted(set(intent.identity.authority_decision_ids) - authorised)
        if unknown:
            return ("BLOCK", "AUTHORITY_NOT_IN_SNAPSHOT",
                    f"J2: {', '.join(unknown)} is not authorised by the signed policy snapshot "
                    f"for this scope; a decision id a caller invents is not a decision record "
                    f"(§1.4, §5.3)")

        if binding["budget_class"] == "EXHAUSTED":
            return ("BLOCK", "BUDGET_EXHAUSTED",
                    "J6: the provider budget is EXHAUSTED; the work is refused rather than "
                    "queued to fail (O9)")

        owner_conflict = (reuse is not None
                          and getattr(reuse.decision, "value", reuse.decision) == "REUSE")
        if owner_conflict:
            # CXC-2: E4's own answer here was a shadow ALLOW on OWNER_CONFLICT.
            # An unapproved reuse of somebody else's capability is the decision a
            # human owns, and no amount of confidence in the match substitutes.
            return ("HUMAN_GATE", "OWNER_CONFLICT",
                    "J5: this intent reuses a capability owned elsewhere with no verified owner "
                    "approval (§6.2); the owner decides, the engine does not")

        work_class = _work_class_value(intent.identity.work_class)
        if degraded and _is_identity_dependent(work_class, reviewer, gate_name, reuse):
            if work_class in IDENTITY_DEPENDENT_WORK_CLASSES:
                # J9 asks *who* may do this. A reviewer does not answer that
                # question — an independent reviewer checks the work, not the
                # entitlement — so this branch does not fall through to REVIEW
                # even when one is named. The owner answers it, or nothing does.
                return ("HUMAN_GATE", "IDENTITY_UNVERIFIED_WHO_MAY_ACT",
                        f"J9/P2a: {work_class} admission is a judgement about which principal "
                        f"may act, and caller_identity_status is UNVERIFIED under a shared uid; "
                        f"the engine has no principal-invariant answer, so the owner decides")
            if reviewer:
                return ("REVIEW", "IDENTITY_UNVERIFIED_REVIEW_REQUIRED",
                        "P2a: the review/test contract is an identity claim and the executor/caller "
                        "binding is UNVERIFIED under a shared uid; admitted only with the named "
                        "independent reviewer")
            return ("HUMAN_GATE", "IDENTITY_UNVERIFIED_NO_REVIEWER",
                    "P2a: an identity-dependent decision under an UNVERIFIED binding with no "
                    "reviewer to name is an owner decision")

        return ("ALLOW", "OK", "every input answered and the contract is satisfiable")

    def _refusal(self, intent: Intent, caller: Caller, ih: str, code: str, text: str,
                 *, conn, unavailable: tuple = ()) -> dict:
        """A BLOCK receipt for a lineage refusal (P2: the audit row exists either way).

        It is deliberately built from the refusal alone and not from the full
        input sweep: the answer does not depend on policy, so reading policy to
        produce it would be spending provider calls to decorate a refusal.
        """
        receipt = {
            "receipt_id": str(uuid.uuid4()), "issued_at": _rfc3339(self._clock()),
            "issuer": self.config.issuer, "task_id": intent.task_id, "intent_hash": ih,
            "parent_receipt_id": intent.parent_receipt_id, "project": intent.identity.project,
            "authority_source": {"decision_ids": sorted(set(intent.identity.authority_decision_ids)),
                                 "tier": None},
            "policy_generation": 0, "policy_hash": "not-consulted",
            "source_decision_revs": [],
            "capability_registry": {"generation": None, "hash": None},
            "matched_capability": None, "reuse": None,
            "runtime_state": "STOPPED",
            "provider_budget": {"provider": None, "state": "UNVERIFIED", "observed_at": None},
            "required_reviewer": None, "required_tester": None,
            "human_gate_state": "NOT_REQUIRED",
            "caller_identity": caller.principal,
            "caller_provenance": getattr(caller.provenance, "value", caller.provenance),
            "executor_binding": None,
            "executor_binding_status": "UNVERIFIED", "caller_identity_status": "UNVERIFIED",
            "downgrade_reason": "SHARED_UID_NO_CREDENTIAL_BOUNDARY",
            "decision": "BLOCK", "reason": {"code": code, "text": text},
            "signature": {"alg": None, "key_id": None, "value": None, "status": "UNVERIFIED"},
            "state": "ISSUED",
            "binding": {"policy_generation": 0, "policy_hash": "not-consulted",
                        "source_decision_revs": [],
                        "capability_registry": {"generation": None, "hash": None},
                        "matched_capability": None, "runtime_state": "STOPPED",
                        "runtime_state_epoch": 0, "human_gate_state": "NOT_REQUIRED",
                        "budget_class": "EXHAUSTED"},
            "idempotency_key": intent.idempotency_key or ih,
            "attempt": 1, "max_attempts": 1, "dispatch_nonces": [], "supersedes": [],
            "provenance": _provenance(intent, None, unavailable),
        }
        receipt = self._resign(receipt)
        receipt_store.record_receipt(conn, receipt, recorded_by="engine", note=code)
        return receipt

    # ── persistence ─────────────────────────────────────────────────────

    def _record(self, conn, receipt: dict, ih: str, *, new_lineage: bool,
                wh: Optional[str] = None) -> Authorization:
        errors = validate_receipt(receipt)
        if errors:
            raise EngineError(f"engine produced a receipt that violates the frozen contract: "
                              f"{errors[0]} ({len(errors)} error(s))")
        receipt_store.record_receipt(conn, receipt, recorded_by="engine")
        if new_lineage and receipt["decision"] != "BLOCK":
            # Atomic claim, never check-then-act: two adapters racing the same
            # intent both reach here, and the PRIMARY KEY decides which one owns
            # the lineage. The loser is told DUPLICATE_INTENT with the winner's id.
            won, holder = receipt_store.claim_lineage(conn, ih, receipt["receipt_id"],
                                                      receipt["project"], receipt["state"],
                                                      work_hash=wh)
            if not won:
                return Authorization(receipt=receipt, http_status=409, code="DUPLICATE_INTENT",
                                     existing_receipt_id=holder)
        status = 201 if receipt["decision"] == "ALLOW" else 403
        return Authorization(receipt=receipt, http_status=status,
                             code=receipt["reason"]["code"])

    # ── signing (P5, O3) ────────────────────────────────────────────────

    def _resign(self, receipt: dict) -> dict:
        """Sign over every field except the signature itself.

        With no key the receipt is explicitly unsigned — ``status: UNVERIFIED``
        with all three fields null — rather than carrying the ``sha256[:16]``
        digest E10 4e already showed is mistaken for integrity.
        """
        body = {k: v for k, v in receipt.items() if k != "signature"}
        if self._key is None:
            receipt["signature"] = {"alg": None, "key_id": None, "value": None,
                                    "status": "UNVERIFIED"}
            return receipt
        mac = hmac.new(self._key, canonical_json(body).encode("utf-8"), hashlib.sha256)
        receipt["signature"] = {
            "alg": "hmac-sha256",
            "key_id": hashlib.sha256(self._key).hexdigest()[:16],
            "value": mac.hexdigest(),
            "status": "VERIFIED",
        }
        return receipt

    def verify(self, receipt: dict) -> bool:
        """Re-check a receipt's own signature. False when unsigned or tampered."""
        sig = receipt.get("signature") or {}
        if self._key is None or sig.get("status") != "VERIFIED" or not sig.get("value"):
            return False
        body = {k: v for k, v in receipt.items() if k != "signature"}
        expected = hmac.new(self._key, canonical_json(body).encode("utf-8"), hashlib.sha256)
        return hmac.compare_digest(expected.hexdigest(), str(sig["value"]))


# ═══════════════════════════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════════════════════════

def _is_identity_dependent(work_class, reviewer, gate_name, reuse) -> bool:
    """Mirrors :func:`agent_crew.cea.validator._identity_dependent` on the issue
    side: what the engine may not ALLOW is exactly what the validator refuses."""
    if work_class in IDENTITY_DEPENDENT_WORK_CLASSES:
        # J9 who-may-do-OPS. The one judgement whose entire content is the
        # principal, so it has no principal-invariant answer to give while
        # identity is UNVERIFIED (P2a).
        return True
    if reviewer:
        return True
    if gate_name != "NOT_REQUIRED":
        return True
    if reuse is not None and getattr(reuse.decision, "value", reuse.decision) in ("REUSE", "EXTEND") \
            and not reuse.approver_identity_verified:
        return True
    return False


def _signature_name(status) -> str:
    return str(getattr(status, "value", status) or "UNSIGNED")


def _denied_gate():
    """A gate provider that could not be consulted is not "no gate required".

    It is recorded as PENDING — an owner decision — rather than NOT_REQUIRED,
    so the fail direction stays the same as every other unreadable input."""
    from agent_crew.cea.receipt import HumanGate, HumanGateState
    return HumanGate(HumanGateState.PENDING)


def _matched(registry: CapabilityLookup):
    matches = tuple(registry.matches) + tuple(registry.anchor_matches)
    return matches[0] if matches else None


def _registry_ref(registry: CapabilityLookup) -> dict:
    """The frozen schema types ``capability_registry.generation`` as integer|null,
    while registries in the field publish dated generations. The string form is
    folded into ``hash`` so drift is still detectable; ``generation`` carries an
    integer only when the registry publishes one."""
    ref = registry.registry
    if ref is None:
        return {"generation": None, "hash": None}
    gen: Any = ref.generation
    raw_hash = ref.hash or ""
    generation = gen if isinstance(gen, int) and not isinstance(gen, bool) else None
    digest = hashlib.sha256(f"{gen}\x00{raw_hash}".encode("utf-8")).hexdigest()
    return {"generation": generation, "hash": digest}


def _gate_json(gate):
    state = getattr(gate.state, "value", gate.state)
    if state == "GRANTED":
        return {"state": "GRANTED", "decision_id": gate.decision_id}
    return state


def _provenance(intent: Intent, snapshot, unavailable) -> dict:
    """§7.2 — ``coordinator_managed`` becomes a named coordinator here and nothing
    more. Provenance is recorded and never read as an admission input.

    ``intent_identity`` is the P4 tuple this receipt was issued over, recorded so
    a later call site can recompute ``B′`` against the *same* scope the engine
    used. ⛔It is recorded, not trusted: it is an input to a provider *lookup*,
    never to a verdict, and it is only as good as the receipt's signature — which
    is why every call site passes ``signature_verification`` alongside it
    (:mod:`agent_crew.cea.callsites`). Without it a validator at CLAIM would have
    to either guess the scope or declare B′ uncomputable for every task, and a
    guessed scope is a B′ that can silently disagree with the one admission used.
    """
    prov: dict = {
        "snapshot_signature_status": (getattr(snapshot.signature, "value", None)
                                      if snapshot is not None else None),
        "coordinator": intent.coordinator_id,
        "intent_identity": _identity_json(intent.identity),
    }
    if unavailable:
        prov["unavailable_inputs"] = list(unavailable)
    if intent.task_type:
        prov["task_type"] = intent.task_type
    return prov


def _identity_json(identity: IntentIdentity) -> dict:
    """The P4 tuple as plain JSON (provenance only — never an admission input)."""
    return {
        "project": identity.project,
        "work_class": _work_class_value(identity.work_class),
        "target": {"repo": identity.target.repo, "base_ref": identity.target.base_ref,
                   "scope_anchors": list(identity.target.scope_anchors)},
        "capability_id": identity.capability_id,
        "authority_decision_ids": list(identity.authority_decision_ids),
    }


def intent_from_receipt(receipt: dict) -> Optional[Intent]:
    """Rebuild the :class:`Intent` a receipt was issued over, or ``None``.

    ``None`` is the honest answer for a receipt that predates
    ``provenance.intent_identity`` (or carries a malformed one): the caller then
    reports the input as unavailable rather than computing ``B′`` over a scope it
    invented. A guessed scope produces a B′ that differs from the admitted one
    for reasons that have nothing to do with drift.
    """
    ident = ((receipt or {}).get("provenance") or {}).get("intent_identity")
    if not isinstance(ident, dict):
        return None
    target = ident.get("target") or {}
    try:
        identity = IntentIdentity(
            project=str(ident["project"]),
            work_class=WorkClass(ident["work_class"]),
            target=Target(repo=str(target.get("repo") or ""),
                          base_ref=str(target.get("base_ref") or ""),
                          scope_anchors=tuple(target.get("scope_anchors") or ())),
            capability_id=ident.get("capability_id"),
            authority_decision_ids=tuple(ident.get("authority_decision_ids") or ()))
    except (KeyError, TypeError, ValueError):
        return None
    return Intent(identity=identity, task_id=str(receipt.get("task_id") or ""),
                  task_type=str(((receipt.get("provenance") or {}).get("task_type")) or ""),
                  description="", coordinator_id=(receipt.get("provenance") or {}).get("coordinator"),
                  parent_receipt_id=receipt.get("parent_receipt_id"))


def _rfc3339(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _load_key(path: Optional[str]) -> Optional[bytes]:
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            data = fh.read().strip()
        return data or None
    except OSError:
        # A configured-but-unreadable key is the degraded case, not a crash: the
        # receipt will say UNVERIFIED, which is the truth an operator needs to see.
        return None


# ═══════════════════════════════════════════════════════════════════════════
# the runtime's engine — in-process today, a socket away from `crew-authz`
# ═══════════════════════════════════════════════════════════════════════════

_ENGINE: Optional[AuthorizationEngine] = None


def get_engine(*, config: Optional[EngineConfig] = None, **providers):
    """The process-wide engine, or a unix-socket client when one is configured.

    §7.1: every adapter calls this and then ``authorize``. Whether that call
    crosses a process boundary is :attr:`EngineConfig.endpoint` and nothing else,
    which is the whole point — deploying ``crew-authz`` must not be a rewrite of
    the twelve ingresses.
    """
    global _ENGINE
    cfg = config or EngineConfig.from_env()
    if cfg.endpoint:
        from agent_crew.cea.service import UnixSocketEngineClient
        return UnixSocketEngineClient(cfg)
    if _ENGINE is None or config is not None or providers:
        engine = AuthorizationEngine(config=cfg, **providers)
        if config is None and not providers:
            _ENGINE = engine
        return engine
    return _ENGINE


def reset_engine() -> None:
    """Drop the cached engine (tests, and config reload)."""
    global _ENGINE
    _ENGINE = None


__all__ = [
    "Authorization", "AuthorizationEngine", "DEFAULT_ROLE_AGENTS", "EMBEDDED_MODES",
    "ENFORCE", "MODES", "TEST", "EngineConfig",
    "EngineError", "REVIEW_FLOOR", "SHADOW", "SnapshotHumanGate", "UnauthenticatedCaller",
    "UnavailableBudget", "UnavailableCapabilityRegistry", "UnavailablePolicySnapshot",
    "UnavailableRuntimeState", "get_engine", "intent_from_receipt", "intent_hash",
    "reset_engine", "work_hash",
]
