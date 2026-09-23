"""Canonical Execution Authorization (CEA) — interface CANDIDATES, not wiring.

Contract: alfred ``sev0/e11-adr-draft`` ``evidence/sev0-p0/E11-ADR-DRAFT.md``
@ ``6cbce56`` ("the ADR"). Section references below (Π P1–P7, §3, §5, §7, §12)
are to that document.

Status (SEV-0 CEA lineage, step 1 of the fold): the contract is FROZEN at
``6cbce565``. ``schema``, ``store`` and the concrete validator in ``validator``
are live code — ``queue.TaskQueue`` now creates the receipt store, and
``/health`` reports the P6 runtime state. The five validator call sites
(enqueue / claim / dispatch / execute start / result) are **not** wired yet;
that is step 2. The dataclasses in ``intent``/``receipt``/``providers`` remain
typed candidates for the engine.

Modules:

* :mod:`agent_crew.cea.intent`         — P4 intent identity, caller principal, ingress adapter ids (§7).
* :mod:`agent_crew.cea.runtime_state`  — P6 ``RuntimeState`` enum + epoch snapshot.
* :mod:`agent_crew.cea.receipt`        — §3 receipt schema incl. Π lifecycle/binding (P3 ``B``).
* :mod:`agent_crew.cea.providers`      — P1 input-provider protocols (data, never verdicts).
* :mod:`agent_crew.cea.validator`      — P2 receipt-validator protocol, five call points.

⛔Still not here (step 2+): the engine ``authorize(intent, caller) -> receipt``
  (P1/T1), ``enqueue_with_receipt`` as the sole ``QUEUED`` writer, the five
  validator call sites, the §7 adapters, and the P2a broker.
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
from agent_crew.cea.schema import SCHEMA_BLOB, SCHEMA_PATH, SCHEMA_SOURCE, validate_receipt
from agent_crew.cea.validator import (
    ContractReceiptValidator, CurrentInputs, O18_IMMEDIATE_INVALIDATION_FIELDS,
    O18_MAX_RECEIPT_AGE_SECONDS, O20_SNAPSHOT_MAX_AGE_SECONDS, ReceiptValidator,
    ValidationOutcome, ValidationPoint, ValidationResult, validate)

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
    # schema (the frozen contract)
    "SCHEMA_BLOB", "SCHEMA_PATH", "SCHEMA_SOURCE", "validate_receipt",
    # validator
    "ContractReceiptValidator", "CurrentInputs", "O18_IMMEDIATE_INVALIDATION_FIELDS",
    "O18_MAX_RECEIPT_AGE_SECONDS", "O20_SNAPSHOT_MAX_AGE_SECONDS", "ReceiptValidator",
    "ValidationOutcome", "ValidationPoint", "ValidationResult", "validate",
]
