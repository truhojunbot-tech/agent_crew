"""Canonical Execution Authorization (CEA) — interface CANDIDATES, not wiring.

Contract: alfred ``sev0/e11-adr-draft`` ``evidence/sev0-p0/E11-ADR-DRAFT.md``
@ ``6cbce56`` ("the ADR"). Section references below (Π P1–P7, §3, §5, §7, §12)
are to that document.

Status (SEV-0 lineage prep, task ``sev0-cea-lineage-prep-r1``): pre-freeze
scaffolding permitted by owner 11354 — typed dataclasses, enums and
``typing.Protocol`` definitions only. Nothing here is imported by the runtime,
migrates a table, or registers a route. Where the ADR is precise the type
mirrors it; where the ADR is silent the field is annotated and the choice is
left to the engine lineage after CONTRACT FREEZE.

Modules:

* :mod:`agent_crew.cea.intent`         — P4 intent identity, caller principal, ingress adapter ids (§7).
* :mod:`agent_crew.cea.runtime_state`  — P6 ``RuntimeState`` enum + epoch snapshot.
* :mod:`agent_crew.cea.receipt`        — §3 receipt schema incl. Π lifecycle/binding (P3 ``B``).
* :mod:`agent_crew.cea.providers`      — P1 input-provider protocols (data, never verdicts).
* :mod:`agent_crew.cea.validator`      — P2 receipt-validator protocol, five call points.

⛔Not here on purpose (starts only after the FROZEN contract commit): the engine
  ``authorize(intent, caller) -> receipt`` (P1/T1), ``enqueue_with_receipt``
  (the sole QUEUED writer), the receipts table/triggers, the §7 adapters, the
  P6 generalisation of ``runtime_stop``, and the P2a broker.
"""
from agent_crew.cea.intent import (
    Caller, CallerProvenance, IdentityStatus, Intent, IntentIdentity, Target, WorkClass)
from agent_crew.cea.providers import (
    BudgetProvider, CallerAuthenticator, CapabilityLookup, CapabilityOwnershipProvider,
    HumanGateProvider, PolicySnapshotProvider, PolicySnapshotRef, RuntimeStateProvider,
    SignatureStatus)
from agent_crew.cea.receipt import (
    Binding, BudgetClass, Decision, DecisionRev, DispatchNonce, DowngradeReason,
    HumanGate, HumanGateState, MatchedCapability, ProviderBudget, Receipt, ReceiptState,
    RegistryRef, Reuse, ReuseDecision)
from agent_crew.cea.runtime_state import RuntimeState, RuntimeStateSnapshot
from agent_crew.cea.validator import (
    ReceiptValidator, ValidationOutcome, ValidationPoint, ValidationResult)

CONTRACT_COMMIT = "6cbce565e6f562c1727fc3fca45f0dac807f0050"
"""alfred ``sev0/e11-adr-draft`` commit these candidates were written against."""

__all__ = [
    "CONTRACT_COMMIT",
    # intent
    "Caller", "CallerProvenance", "IdentityStatus", "Intent", "IntentIdentity", "Target", "WorkClass",
    # runtime state
    "RuntimeState", "RuntimeStateSnapshot",
    # receipt
    "Binding", "BudgetClass", "Decision", "DecisionRev", "DispatchNonce", "DowngradeReason",
    "HumanGate", "HumanGateState", "MatchedCapability", "ProviderBudget", "Receipt",
    "ReceiptState", "RegistryRef", "Reuse", "ReuseDecision",
    # providers
    "BudgetProvider", "CallerAuthenticator", "CapabilityLookup", "CapabilityOwnershipProvider",
    "HumanGateProvider", "PolicySnapshotProvider", "PolicySnapshotRef", "RuntimeStateProvider",
    "SignatureStatus",
    # validator
    "ReceiptValidator", "ValidationOutcome", "ValidationPoint", "ValidationResult",
]
