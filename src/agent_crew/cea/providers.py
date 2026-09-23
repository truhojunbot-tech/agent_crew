"""P1 input providers — return data, never a verdict (ADR Π P1, §2.3, §5, §6.5).

"Everything else is one of exactly two kinds: an input provider (snapshot
producer, E4 registry, runtime state, budget observation, caller authentication)
that returns data and never a verdict; or the receipt validator (P2)."

Each protocol below is one row of §2.3's input column. None of them returns a
:class:`~agent_crew.cea.receipt.Decision`; the engine composes them. Fail
direction on unavailability is P7 and belongs to the engine, so a provider
reports *unavailable / stale* explicitly instead of guessing.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol, runtime_checkable

from agent_crew.cea.intent import Caller, Intent
from agent_crew.cea.receipt import DecisionRev, HumanGate, MatchedCapability, ProviderBudget, RegistryRef
from agent_crew.cea.runtime_state import RuntimeStateSnapshot


class SignatureStatus(str, Enum):
    """How a signed input verified. ``UNKEYED`` is E10 4e's ``sha256[:16]`` digest: not integrity."""
    VALID = "VALID"
    INVALID = "INVALID"
    UNKEYED = "UNKEYED"
    UNSIGNED = "UNSIGNED"


@dataclass(frozen=True)
class CapabilityLookup:
    """§11.1 row 2: ``lookup(intent) → {matches, owner, generation, hash}``.

    ``available=False`` or ``stale=True`` ⇒ engine BLOCK ``REGISTRY_UNAVAILABLE``
    (§5.4, P7, CX-3 / CXC-2). ``anchor_matches`` are §5.2 artefact-anchor hits
    (CX-4i(b)); ``matches`` are capability-id/alias hits.
    """
    registry: Optional[RegistryRef]
    matches: tuple[MatchedCapability, ...] = ()
    anchor_matches: tuple[MatchedCapability, ...] = ()
    available: bool = True
    stale: bool = False
    signature: SignatureStatus = SignatureStatus.UNSIGNED


@dataclass(frozen=True)
class PolicySnapshotRef:
    """§5.3 canonical snapshot as the engine sees it (one signed file per generation).

    ``decisions`` are the structured records (§1.4) the snapshot lists;
    ``in_scope`` narrows them to the intent's capability/project for J2/J3.
    """
    generation: int
    hash: str
    produced_at: Optional[float]
    decisions: tuple[DecisionRev, ...]
    in_scope: tuple[DecisionRev, ...] = ()
    signature: SignatureStatus = SignatureStatus.UNSIGNED
    age_seconds: Optional[float] = None    # vs snapshot_max_age (O20) when the SSOT is unreachable
    available: bool = True


@runtime_checkable
class CapabilityOwnershipProvider(Protocol):
    """§5.1 E4 — "the only capability matcher" (J4, J5); called only by the engine."""

    def lookup(self, intent: Intent) -> CapabilityLookup: ...


@runtime_checkable
class PolicySnapshotProvider(Protocol):
    """§5.3 snapshot reader (J2, J3). Verifies the producer signature before use (§3)."""

    def current(self, intent: Optional[Intent] = None) -> PolicySnapshotRef: ...


@runtime_checkable
class RuntimeStateProvider(Protocol):
    """P6 row reader (J6). Unreadable ⇒ ``read_failed=True`` (engine treats as STOPPED, P7)."""

    def current(self) -> RuntimeStateSnapshot: ...


@runtime_checkable
class BudgetProvider(Protocol):
    """J6 provider budget from Qouta (G16 as an input, never a gate; O9 fail direction)."""

    def budget(self, provider: str) -> ProviderBudget: ...


@runtime_checkable
class HumanGateProvider(Protocol):
    """J8 — from snapshot ``human_gate_predicates`` / decision records only, never a self-asserted flag."""

    def state(self, intent: Intent, snapshot: PolicySnapshotRef) -> HumanGate: ...


@runtime_checkable
class CallerAuthenticator(Protocol):
    """§6.5 / J9 — maps a presented credential to a :class:`Caller`.

    Returns ``None`` for no/invalid credential (⇒ 401, CXC-4). Under the shared
    uid the result carries ``identity_status=UNVERIFIED`` (P2a); ``VERIFIED``
    comes only from the broker's spawn/registration path (O21b).
    """

    def authenticate(self, credential: Optional[str]) -> Optional[Caller]: ...
