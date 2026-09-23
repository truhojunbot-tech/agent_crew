"""§3 authorization receipt schema, incl. Π lifecycle and binding fields (ADR §3, P2, P3, P4).

Pure types. The receipts table (``authorization_receipts``, append-only with
UPDATE/DELETE-blocking triggers, unique partial index on ``intent_hash`` for
live lineages) and the signing scheme are engine work after freeze.

Field names follow the §3 YAML block verbatim so a stored receipt row, this
dataclass and the ADR can be diffed by eye.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from agent_crew.cea.intent import CallerProvenance, IdentityStatus
from agent_crew.cea.runtime_state import RuntimeState


class Decision(str, Enum):
    """§2.3: ``ALLOW | BLOCK | REVIEW | HUMAN_GATE``.

    ``REVIEW`` = admit only with a named independent reviewer before execution;
    ``HUMAN_GATE`` = owner decision required, nothing runs.
    """
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    REVIEW = "REVIEW"
    HUMAN_GATE = "HUMAN_GATE"


class ReceiptState(str, Enum):
    """§3 lifecycle ``state``.

    Live lineage (occupies the P4 ``intent_hash`` partial index):
    ISSUED, QUEUED, CLAIMED, RUNNING, HELD. Terminal: CONSUMED (P4 completed
    work), SUPERSEDED (P3 re-admission), REVOKED.
    """
    ISSUED = "ISSUED"
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    HELD = "HELD"
    CONSUMED = "CONSUMED"
    SUPERSEDED = "SUPERSEDED"
    REVOKED = "REVOKED"


LIVE_STATES = frozenset({ReceiptState.ISSUED, ReceiptState.QUEUED, ReceiptState.CLAIMED,
                         ReceiptState.RUNNING, ReceiptState.HELD})
"""P4: lineage states that occupy the unique partial index on ``intent_hash``."""


class HumanGateState(str, Enum):
    """§3 ``human_gate_state: NOT_REQUIRED | PENDING | GRANTED{decision_id} | DENIED``."""
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    GRANTED = "GRANTED"
    DENIED = "DENIED"


class BudgetClass(str, Enum):
    """P3 binding ``budget_class (OK | CONSTRAINED | EXHAUSTED)`` — J6 input from Qouta (G16 as input, O9)."""
    OK = "OK"
    CONSTRAINED = "CONSTRAINED"
    EXHAUSTED = "EXHAUSTED"


class ReuseDecision(str, Enum):
    """§3 ``reuse.decision: NEW | REUSE | EXTEND``."""
    NEW = "NEW"
    REUSE = "REUSE"
    EXTEND = "EXTEND"


class DowngradeReason(str, Enum):
    """§3 ``downgrade_reason`` (P2a). Open enum: the ADR writes ``| ...``."""
    SHARED_UID_NO_CREDENTIAL_BOUNDARY = "SHARED_UID_NO_CREDENTIAL_BOUNDARY"


@dataclass(frozen=True)
class DecisionRev:
    """§3 ``source_decision_revs: [{decision_id, body_hash}]`` — edits invalidate (§1.4, CX-4g)."""
    decision_id: str
    body_hash: str
    supersedes: tuple[str, ...] = ()
    """Decision ids this record **explicitly** supersedes (P4's ALREADY_COMPLETED exception).

    Only the signed snapshot fills this. It is deliberately not part of the
    ``source_decision_revs`` the receipt emits — that stays the frozen
    ``{decision_id, body_hash}`` pair — because it is an input to admission, not
    a field of the binding.
    """


@dataclass(frozen=True)
class RegistryRef:
    """§3 ``capability_registry: {generation, hash}`` (E4, §5)."""
    generation: str
    hash: str


@dataclass(frozen=True)
class MatchedCapability:
    """§3 ``matched_capability: {id, owner, repo} | null``."""
    id: str
    owner: str
    repo: str


@dataclass(frozen=True)
class Reuse:
    """§3 ``reuse: {decision, approved_by, approver_identity_verified}`` (§6.2: owner identity, never free text)."""
    decision: ReuseDecision
    approved_by: Optional[str] = None
    approver_identity_verified: bool = False


@dataclass(frozen=True)
class ProviderBudget:
    """§3 ``provider_budget: {provider, state, observed_at}``."""
    provider: str
    state: BudgetClass
    observed_at: Optional[float] = None


@dataclass(frozen=True)
class HumanGate:
    """§3 ``human_gate_state`` with the T0 ``decision_id`` that granted it (J8 reads snapshot records only)."""
    state: HumanGateState
    decision_id: Optional[str] = None


@dataclass(frozen=True)
class Binding:
    """P3 binding tuple ``B``, recomputed as ``B′`` at claim / dispatch / execute start.

    ::

        B = { policy_generation, policy_hash, source_decision_revs[{decision_id, body_hash}],
              capability_registry{generation, hash}, matched_capability{id, owner},
              runtime_state, runtime_state_epoch, human_gate_state (+ decision_id),
              budget_class (OK | CONSTRAINED | EXHAUSTED) }

    Outcome per changed field is the P3 table (re-admit / block / human gate /
    HELD-deferred); if ``B′`` cannot be computed ⇒ P7.
    """
    policy_generation: int
    policy_hash: str
    source_decision_revs: tuple[DecisionRev, ...]
    capability_registry: RegistryRef
    matched_capability: Optional[MatchedCapability]
    runtime_state: RuntimeState
    runtime_state_epoch: int
    human_gate: HumanGate
    budget_class: BudgetClass


@dataclass(frozen=True)
class DispatchNonce:
    """§3 ``dispatch_nonces: [{nonce, attempt, issued_at, used_at}]`` — single-use (P2 dispatch row, P4)."""
    nonce: str
    attempt: int
    issued_at: float
    used_at: Optional[float] = None


@dataclass(frozen=True)
class Receipt:
    """The §3 receipt: "the only token that lets a task enter QUEUED, be claimed, or be dispatched".

    Grouped as in §3. ``signature`` covers every field above it; it is produced
    by the engine key (P5, O3) — under the shared uid the scheme is
    tamper-evident, not tamper-proof, and the receipt says so through
    ``executor_binding_status`` / ``caller_identity_status`` / ``downgrade_reason`` (P2a).
    """
    # --- identity ---
    receipt_id: str
    issued_at: str                                 # RFC3339 UTC
    issuer: str                                    # engine instance id (runtime port + build commit)
    # --- what ---
    task_id: str
    intent_hash: str                               # J1 / P4
    project: str
    parent_receipt_id: Optional[str] = None        # lineage root for cascades / retries
    # --- authority ---
    authority_source: tuple[str, ...] = ()         # decision_id(s) + tier that authorise this intent
    policy_generation: int = 0
    policy_hash: str = ""
    source_decision_revs: tuple[DecisionRev, ...] = ()
    capability_registry: Optional[RegistryRef] = None
    matched_capability: Optional[MatchedCapability] = None
    reuse: Optional[Reuse] = None
    # --- runtime ---
    runtime_state: RuntimeState = RuntimeState.STOPPED   # at issue time; STOPPED is the fail-closed default (P7)
    provider_budget: Optional[ProviderBudget] = None
    # --- contract ---
    required_reviewer: Optional[str] = None        # provider/identity class ≠ executor (J7; `coordinator_managed` never reduces it)
    required_tester: Optional[str] = None
    human_gate_state: HumanGate = HumanGate(HumanGateState.NOT_REQUIRED)
    # --- who ---
    caller_identity: str = ""                      # authenticated principal (§6.5)
    caller_provenance: CallerProvenance = CallerProvenance.DIRECT
    executor_binding: str = ""                     # provider/agent allowed to claim; result must come from it
    executor_binding_status: IdentityStatus = IdentityStatus.UNVERIFIED   # VERIFIED written only by the P2a broker
    caller_identity_status: IdentityStatus = IdentityStatus.UNVERIFIED
    downgrade_reason: Optional[DowngradeReason] = DowngradeReason.SHARED_UID_NO_CREDENTIAL_BOUNDARY
    # --- verdict ---
    decision: Decision = Decision.BLOCK            # fail-closed default; the engine sets it
    reason: str = ""                               # machine code + text
    # --- lifecycle and binding (Π additions to §3) ---
    state: ReceiptState = ReceiptState.ISSUED
    binding: Optional[Binding] = None
    idempotency_key: Optional[str] = None
    attempt: int = 0
    max_attempts: int = 1                          # from the snapshot (P4)
    dispatch_nonces: tuple[DispatchNonce, ...] = ()
    supersedes: tuple[str, ...] = ()               # receipt_ids; only via a newer decision record (P4)
    # --- integrity ---
    signature: str = ""
    signature_key_id: Optional[str] = None        # engine key id; None ⇒ unsigned / in-process degraded (P2a)
    extra: dict = field(default_factory=dict)      # provenance only (e.g. L0 persistence evidence)
