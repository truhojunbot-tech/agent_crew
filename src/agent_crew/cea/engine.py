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
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from agent_crew.cea.intent import (
    Caller, IdentityStatus, Intent, IntentIdentity, InvalidScopeAnchor, WorkClass,
    canonical_identity)
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

SHADOW = "shadow"
ENFORCE = "enforce"


@dataclass(frozen=True)
class EngineConfig:
    """How this runtime reaches the engine, and what the adapters do with it.

    ``mode`` is read by the **adapters**, never by :meth:`AuthorizationEngine.authorize`.
    In ``shadow`` an adapter records the receipt and proceeds as it does today; in
    ``enforce`` a non-ALLOW receipt stops the work. The engine's verdict is
    identical either way — that is what makes the shadow measurement worth
    anything, and it is why "shadow-ALLOW because an input was missing" cannot
    happen: the engine has no shadow branch to take.
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

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "EngineConfig":
        e = os.environ if env is None else env
        mode = (e.get("AGENT_CREW_CEA_MODE") or SHADOW).strip().lower()
        if mode not in (SHADOW, ENFORCE):
            mode = SHADOW
        return cls(
            mode=mode,
            endpoint=(e.get("AGENT_CREW_CEA_ENGINE_ENDPOINT") or "").strip() or None,
            key_path=(e.get("AGENT_CREW_CEA_ENGINE_KEY") or "").strip() or None,
            issuer=(e.get("AGENT_CREW_CEA_ISSUER") or "").strip() or cls.issuer,
            caller_token_path=(e.get("AGENT_CREW_CEA_ADAPTER_TOKEN_FILE") or "").strip() or None,
        )

    @property
    def enforcing(self) -> bool:
        return self.mode == ENFORCE


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


_TRUSTED_CREDENTIAL_KINDS = ("adapter_token", "broker_registered")
"""The kinds :mod:`agent_crew.cea.auth` can issue. ``None`` is not one of them."""


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

    # ── public API ──────────────────────────────────────────────────────

    def authorize(self, conn: sqlite3.Connection, intent: Intent, caller: Caller, *,
                  retry: bool = False) -> Authorization:
        """Decide whether this intent may exist, and record the receipt.

        Returns an :class:`Authorization` whose ``http_status`` is the adapter's
        answer: ``201`` newly admitted, ``200`` the same receipt returned for an
        idempotent replay, ``409`` ``DUPLICATE_INTENT``, ``403`` blocked/held,
        ``401`` no caller credential.
        """
        receipt_store.ensure_schema(conn)

        # J9 — who is asking. 401 before anything else is read: an
        # unauthenticated caller does not get to learn the policy state, and it
        # does not get an audit row in its chosen name either.
        #
        # ⛔A Caller is only ever a *result of authentication*
        #   (:mod:`agent_crew.cea.auth`). Checking `caller is None` was not
        #   authentication: the socket decoder built one from request JSON, so
        #   `Caller(principal="attacker", provenance="direct",
        #   credential_kind=None)` passed this line and an OPS intent under it
        #   was ALLOWed (codex P1 #1). The credential_kind test below is the
        #   in-process half of the fix — an object nobody authenticated has no
        #   kind to name — and the socket half is that the wire no longer
        #   carries a principal at all.
        if caller is None:
            raise UnauthenticatedCaller(
                "authorize() requires an authenticated Caller; adapters authenticate first (§7.1)")
        if getattr(caller, "credential_kind", None) not in _TRUSTED_CREDENTIAL_KINDS:
            raise UnauthenticatedCaller(
                f"caller {caller.principal!r} presents credential_kind "
                f"{getattr(caller, 'credential_kind', None)!r}, which no authenticator issues; "
                f"a Caller must come from agent_crew.cea.auth, never from a request body (J9)")

        # P4/§5.2: one spelling per target, fixed *before* the hash and before
        # the E4 registry sees the intent — so identity and matching can never
        # disagree about which file was named.
        try:
            intent = replace(intent, identity=canonical_identity(intent.identity))
        except InvalidScopeAnchor as exc:
            return Authorization(
                receipt=self._refusal(intent, caller, uncanonical_intent_hash(intent.identity),
                                      "INVALID_SCOPE_ANCHOR", str(exc), conn=conn),
                http_status=400, code="INVALID_SCOPE_ANCHOR")
        ih = intent_hash(intent.identity)

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

        unavailable = self._unavailable_inputs(snapshot, registry, runtime) + raised
        reviewer, tester = self._j7_contract(intent, snapshot, executor)
        reuse = self._j5_reuse(intent, caller, registry)

        receipt = self._build(
            intent=intent, caller=caller, intent_hash_value=ih, snapshot=snapshot,
            registry=registry, runtime=runtime, budget=budget, gate=gate,
            executor=executor, reviewer=reviewer, tester=tester, reuse=reuse,
            unavailable=unavailable)

        return self._record(conn, receipt, ih, new_lineage=True)

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
                   note: Optional[str] = None) -> dict:
        """Append a lifecycle row and keep the P4 lineage claim in step with it."""
        current = receipt_store.current_receipt(conn, receipt_id)
        if current is None:
            raise EngineError(f"unknown receipt_id {receipt_id!r}")
        updated = self._resign(dict(current, state=state))
        receipt_store.record_receipt(conn, updated, recorded_by="engine", note=note)
        receipt_store.set_lineage_state(conn, updated["intent_hash"], receipt_id, state)
        return updated

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

    def _j5_reuse(self, intent: Intent, caller: Caller, registry: CapabilityLookup):
        """J4/J5 — the registry matched something someone else owns (§6.2, CX-4i).

        Reuse is only ever *recorded* here. Approval is an owner act with a
        verified identity; under P2a nothing in this process can verify one, so
        ``approver_identity_verified`` is False and the decision path treats the
        receipt as identity-dependent — which is exactly why it cannot be ALLOW.
        """
        from agent_crew.cea.receipt import Reuse, ReuseDecision
        matches = tuple(registry.matches) + tuple(registry.anchor_matches)
        if not matches:
            return Reuse(ReuseDecision.NEW) if registry.available else None
        owner = matches[0].owner
        if owner and owner != caller.principal and owner != intent.identity.project:
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
        #   which does not exist yet — so in this build both statuses are always
        #   UNVERIFIED, and `_broker_verified` is the single place that will
        #   change when the broker lands.
        caller_status = _broker_verified(caller)
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

        if not (snapshot.in_scope or snapshot.decisions):
            return ("BLOCK", "NO_AUTHORITY",
                    "J2: the policy snapshot lists no decision in scope for this intent; "
                    "free text in a task description is not authority (§1.4)")

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

        if degraded and _is_identity_dependent(reviewer, gate_name, reuse):
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
                 *, conn) -> dict:
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
            "provenance": _provenance(intent, None, ()),
        }
        receipt = self._resign(receipt)
        receipt_store.record_receipt(conn, receipt, recorded_by="engine", note=code)
        return receipt

    # ── persistence ─────────────────────────────────────────────────────

    def _record(self, conn, receipt: dict, ih: str, *, new_lineage: bool) -> Authorization:
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
                                                      receipt["project"], receipt["state"])
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

def _is_identity_dependent(reviewer, gate_name, reuse) -> bool:
    """Mirrors :func:`agent_crew.cea.validator._identity_dependent` on the issue
    side: what the engine may not ALLOW is exactly what the validator refuses."""
    if reviewer:
        return True
    if gate_name != "NOT_REQUIRED":
        return True
    if reuse is not None and getattr(reuse.decision, "value", reuse.decision) in ("REUSE", "EXTEND") \
            and not reuse.approver_identity_verified:
        return True
    return False


def _broker_verified(caller: Caller) -> IdentityStatus:
    """P2a: VERIFIED only from independently verified broker evidence (O21b).

    Two conditions, both required, neither satisfiable by anything a caller
    sends: the credential must be the broker's registration kind, and only
    :mod:`agent_crew.cea.auth` issues credential kinds. A token file is
    tamper-evident under one uid, which is not the same claim.
    """
    from agent_crew.cea.auth import CREDENTIAL_KIND_BROKER
    if (getattr(caller, "credential_kind", None) == CREDENTIAL_KIND_BROKER
            and caller.identity_status is IdentityStatus.VERIFIED):
        return IdentityStatus.VERIFIED
    return IdentityStatus.UNVERIFIED


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
    more. Provenance is recorded and never read as an admission input."""
    prov: dict = {
        "snapshot_signature_status": (getattr(snapshot.signature, "value", None)
                                      if snapshot is not None else None),
        "coordinator": intent.coordinator_id,
    }
    if unavailable:
        prov["unavailable_inputs"] = list(unavailable)
    if intent.task_type:
        prov["task_type"] = intent.task_type
    return prov


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
    "Authorization", "AuthorizationEngine", "DEFAULT_ROLE_AGENTS", "ENFORCE", "EngineConfig",
    "EngineError", "REVIEW_FLOOR", "SHADOW", "SnapshotHumanGate", "UnavailableBudget",
    "UnavailableCapabilityRegistry", "UnavailablePolicySnapshot", "UnavailableRuntimeState",
    "get_engine", "intent_hash", "reset_engine",
]
