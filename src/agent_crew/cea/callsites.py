"""P2's five call sites — the only places a receipt is checked (ADR Π P2, P3, P6, §11.2 T3).

P2: *"one receipt validator implementation ... without a valid receipt there is
no enqueue, no claim, no dispatch, no execute."* This module is the seam that
makes the second half of that sentence checkable: :data:`VALIDATOR` is the one
validator object in the product, and the five functions below are the only
callers of its five methods. ``tests/unit/test_sev0_cea_s2c_writer_callsites.py``
asserts both statically — a sixth call site, or a second validator instance, is a
test failure rather than a thing somebody notices in review later.

=============  ===============================================================
point          adapter
=============  ===============================================================
ENQUEUE        ``queue.TaskQueue.enqueue_with_receipt`` — inside the same
               ``BEGIN IMMEDIATE`` that reads ``runtime_stop`` and writes the row
CLAIM          ``queue.TaskQueue.dequeue`` critical section
DISPATCH       ``queue.TaskQueue.record_dispatch`` — where the push/spawn path
               decides, and where the single-use nonce is minted
EXECUTE_START  ``queue.TaskQueue.start_execution`` — one-shot, pre-spawn;
               the pane calls ``POST /tasks/{id}/start {nonce}`` for a go/no-go
RESULT         ``queue.TaskQueue.submit_result`` — nonce + ``executor_binding``
=============  ===============================================================

**Shadow vs enforce lives here and nowhere else.** The engine always decides
truthfully (P7: no shadow branch), and the validator always answers truthfully;
what a deployment chooses is whether a non-PROCEED answer *stops the work*. In
``shadow`` every gate returns ``proceed=True`` with ``enforced=False`` and the
answer is recorded — which is the measurement the shadow phase exists to produce.
In ``enforce`` only ``PROCEED`` proceeds.

⛔A gate never invents a verdict when it cannot compute one. If ``B′`` is not
  available the :class:`~agent_crew.cea.validator.CurrentInputs` says so and the
  validator applies P7's asymmetry (fail closed at admission, hold work already
  authorised). An adapter that filled in a plausible B′ instead would turn every
  drift check into a comparison of the receipt with itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from agent_crew.cea.engine import ENFORCE, TEST, EngineConfig
from agent_crew.cea.validator import (
    ContractReceiptValidator, CurrentInputs, ReceiptValidator, ValidationOutcome,
    ValidationPoint, ValidationResult)

VALIDATOR: ReceiptValidator = ContractReceiptValidator()
"""T3 — the single receipt-validator instance in the product.

One object, so "the validator" is a thing that can be pointed at rather than a
policy re-implemented per call site. The static test asserts no other module
constructs a :class:`~agent_crew.cea.validator.ContractReceiptValidator`.
"""


@dataclass(frozen=True)
class GateOutcome:
    """What a call site learned, and whether it is allowed to act on it."""
    point: ValidationPoint
    result: ValidationResult
    proceed: bool
    enforced: bool

    @property
    def outcome(self) -> ValidationOutcome:
        return self.result.outcome

    @property
    def reason(self) -> str:
        return self.result.reason

    @property
    def receipt_id(self) -> str:
        return self.result.receipt_id

    def as_record(self) -> dict:
        """The audit shape an adapter stores/logs — the answer, and what was done with it."""
        return {"point": self.point.value, "outcome": self.outcome.value,
                "reason": self.reason, "receipt_id": self.receipt_id,
                "proceed": self.proceed, "enforced": self.enforced,
                "changed_fields": list(self.result.changed_fields)}


def enforcing(config: Optional[EngineConfig] = None, *,
              project: Optional[str] = None) -> bool:
    """``test`` enforces exactly as ``enforce`` does — the two differ only in
    whether the engine may run embedded (:data:`agent_crew.cea.engine.EMBEDDED_MODES`),
    which is a deployment question and not a question about this gate.

    ``project`` selects the per-project rollout override
    (:func:`agent_crew.cea.engine.resolve_mode`) and is ignored when an explicit
    ``config`` is given — a caller holding a config has already resolved the
    question, and re-resolving it here would silently override them.
    """
    return (config or EngineConfig.from_env(project=project)).mode in (ENFORCE, TEST)


def recording(config: Optional[EngineConfig] = None, *,
              project: Optional[str] = None) -> bool:
    """Is a gate answer computed at all for this project? ``off`` says no."""
    return (config or EngineConfig.from_env(project=project)).recording


def _gate(point: ValidationPoint, result: ValidationResult,
          config: Optional[EngineConfig]) -> GateOutcome:
    enforce = enforcing(config)
    proceed = (result.outcome is ValidationOutcome.PROCEED) or not enforce
    return GateOutcome(point=point, result=result, proceed=proceed, enforced=enforce)


def current_inputs(engine, receipt: dict, **overrides) -> CurrentInputs:
    """Build :class:`CurrentInputs` for a receipt already in flight.

    One place, so the four post-admission call sites cannot drift in what they
    tell the validator. ``signature_verification`` is the engine's own
    ``verify()`` — a *verifier's* answer, which is the only thing P2a lets the
    validator treat as verified; the receipt's own ``signature.status`` is the
    caller's claim about itself and is ignored here on purpose.
    """
    binding, unavailable = engine.current_binding(receipt)
    verified = None
    try:
        verified = engine.verify(receipt)
    except AttributeError:      # a remote client has no key and says so by omission
        verified = None
    return CurrentInputs(binding=binding, unavailable_inputs=unavailable,
                         signature_verification=verified, **overrides)


# ── the five call sites ─────────────────────────────────────────────────────

def gate_enqueue(receipt: dict, *, task_id: str, current: CurrentInputs,
                 config: Optional[EngineConfig] = None) -> GateOutcome:
    """P2 Enqueue. The only gate that fails *closed* on an uncomputable B′ (P7)."""
    return _gate(ValidationPoint.ENQUEUE,
                 VALIDATOR.validate_enqueue(receipt, task_id=task_id, current=current),
                 config)


def gate_claim(receipt: dict, *, claimant: Optional[str], current: CurrentInputs,
               config: Optional[EngineConfig] = None) -> GateOutcome:
    """P2 Claim. ``claimant`` is an *asserted* identity under P2a — tamper-evident,
    not proven, and the receipt records that it is not."""
    return _gate(ValidationPoint.CLAIM,
                 VALIDATOR.validate_claim(receipt, claimant=claimant, current=current),
                 config)


def gate_dispatch(receipt: dict, *, attempt: Optional[int], current: CurrentInputs,
                  config: Optional[EngineConfig] = None) -> GateOutcome:
    """P2 Dispatch. The nonce is minted by the caller *after* this answers PROCEED,
    because minting is a write and a refused dispatch must not leave one behind."""
    return _gate(ValidationPoint.DISPATCH,
                 VALIDATOR.validate_dispatch(receipt, attempt=attempt, current=current),
                 config)


def gate_execute_start(receipt: dict, *, nonce: Optional[str], current: CurrentInputs,
                       config: Optional[EngineConfig] = None) -> GateOutcome:
    """P2 Execute start — one-shot, pre-spawn. ``current.nonce_unused`` comes from
    the store's claim table, never from the receipt's own ``dispatch_nonces``."""
    return _gate(ValidationPoint.EXECUTE_START,
                 VALIDATOR.validate_execute_start(receipt, nonce=nonce, current=current),
                 config)


def gate_result(receipt: dict, *, nonce: Optional[str], presenter: Optional[str],
                current: CurrentInputs, config: Optional[EngineConfig] = None) -> GateOutcome:
    """§2.2 / P2 last paragraph — ``/result`` with the dispatch nonce under
    ``executor_binding``. P3: drift here never kills in-flight work; it flags it."""
    return _gate(ValidationPoint.RESULT,
                 VALIDATOR.validate_result(receipt, nonce=nonce, presenter=presenter,
                                           current=current),
                 config)


CALL_SITES = {
    ValidationPoint.ENQUEUE: gate_enqueue,
    ValidationPoint.CLAIM: gate_claim,
    ValidationPoint.DISPATCH: gate_dispatch,
    ValidationPoint.EXECUTE_START: gate_execute_start,
    ValidationPoint.RESULT: gate_result,
}
"""Every :class:`ValidationPoint` has exactly one gate — checked by the I2 test."""


__all__ = ["CALL_SITES", "GateOutcome", "VALIDATOR", "current_inputs", "enforcing",
           "recording",
           "gate_claim", "gate_dispatch", "gate_enqueue", "gate_execute_start", "gate_result"]
