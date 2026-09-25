"""Canonical Execution Authorization (CEA) — the T1 engine and its contract.

Contract: alfred ``sev0/e11-adr-draft`` ``evidence/sev0-p0/E11-ADR-DRAFT.md``
@ ``6cbce56`` ("the ADR"). Section references below (Π P1–P7, §3, §5, §7, §12)
are to that document.

Status (SEV-0 CEA lineage, step 2 of the fold): the contract is FROZEN at
``6cbce565``. ``schema``, ``store``, ``validator`` and now ``engine`` are live
code — ``queue.TaskQueue`` creates the receipt store, ``/health`` reports the P6
runtime state, and :func:`~agent_crew.cea.engine.AuthorizationEngine.authorize`
turns an intent plus a caller into a schema-valid receipt.

⛔Still ahead (step 2c/2d): ``enqueue_with_receipt`` as the sole ``QUEUED``
  writer with ``tasks.receipt_id NOT NULL``; the five validator call sites
  (enqueue / claim / dispatch / execute start / result); the §7 adapters at the
  twelve ingresses; and the bypass removals (cascade suppression, the #294
  lexical advisory, ``risk_tier.py`` as a decision). Until those land the engine
  decides correctly but nothing calls it, so admission behaviour is unchanged.

Modules:

* :mod:`agent_crew.cea.intent`         — P4 intent identity, caller principal, ingress adapter ids (§7).
* :mod:`agent_crew.cea.runtime_state`  — P6 ``RuntimeState`` enum + epoch snapshot.
* :mod:`agent_crew.cea.receipt`        — §3 receipt schema incl. Π lifecycle/binding (P3 ``B``).
* :mod:`agent_crew.cea.providers`      — P1 input-provider protocols (data, never verdicts).
* :mod:`agent_crew.cea.validator`      — P2 receipt-validator protocol, five call points.
* :mod:`agent_crew.cea.engine`         — T1 ``authorize(intent, caller) -> receipt`` (J1–J9, P4, P7, P2a).
* :mod:`agent_crew.cea.service`        — the unix-socket boundary (``crew-authz`` is a config change).

⛔Not here, and not pretended to be: the P2a broker. ``VERIFIED`` identity needs
  distinct uids; every receipt this engine issues says ``UNVERIFIED`` with
  ``downgrade_reason`` until that exists.
"""
from agent_crew.cea.engine import (
    Authorization, AuthorizationEngine, EngineConfig, EngineError, REVIEW_FLOOR,
    get_engine, intent_hash)
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
    ATTESTABLE_IDENTITIES, ContractReceiptValidator, CurrentInputs, O18_IMMEDIATE_INVALIDATION_FIELDS,
    O18_MAX_RECEIPT_AGE_SECONDS, O20_SNAPSHOT_MAX_AGE_SECONDS, ReceiptValidator,
    ValidationOutcome, ValidationPoint, ValidationResult, validate)

CONTRACT_COMMIT = "6cbce565e6f562c1727fc3fca45f0dac807f0050"
"""alfred ``sev0/e11-adr-draft`` commit these candidates were written against."""

__all__ = [
    "CONTRACT_COMMIT",
    # engine (T1)
    "Authorization", "AuthorizationEngine", "EngineConfig", "EngineError", "REVIEW_FLOOR",
    "get_engine", "intent_hash",
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
    "ATTESTABLE_IDENTITIES", "ContractReceiptValidator", "CurrentInputs",
    "O18_IMMEDIATE_INVALIDATION_FIELDS",
    "O18_MAX_RECEIPT_AGE_SECONDS", "O20_SNAPSHOT_MAX_AGE_SECONDS", "ReceiptValidator",
    "ValidationOutcome", "ValidationPoint", "ValidationResult", "validate",
]
