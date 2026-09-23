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
    ContractReceiptValidator, CurrentInputs, ReceiptValidator, RuntimeStateVerdict,
    ValidationOutcome, ValidationPoint, ValidationResult, binding_drifted,
    runtime_state_verdict)

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


# ── P6 runtime state: the row is an input, not a second authority ───────────

@dataclass(frozen=True)
class RuntimeGateOutcome:
    """What a runtime-state call site learned. It did not work it out itself."""
    point: ValidationPoint
    verdict: RuntimeStateVerdict

    @property
    def proceed(self) -> bool:
        return self.verdict.permits

    @property
    def state(self) -> str:
        return self.verdict.state

    @property
    def outcome(self) -> ValidationOutcome:
        return self.verdict.outcome

    @property
    def reason(self) -> str:
        return self.verdict.reason

    def as_record(self) -> dict:
        return {"point": self.point.value, "runtime_state": self.state,
                "outcome": self.outcome.value, "code": self.verdict.code,
                "reason": self.reason, "proceed": self.proceed}


def gate_runtime_state(runtime_state: str, *, point: ValidationPoint,
                       already_claimed: bool = False,
                       already_dispatched: bool = False) -> RuntimeGateOutcome:
    """P6: may ``point`` proceed while the runtime row says ``runtime_state``?

    This is the seam that stops ``queue.py`` owning a runtime-state judgement of
    its own. ``_stop_active_in_txn`` used to answer ``state != "ACTIVE"`` inline,
    which is a second implementation of the subject matter T3 already covers
    (guard inventory §11.2 #12). The queue now reads the row — it is the only
    thing that can, inside its own ``BEGIN IMMEDIATE`` — and relays the answer
    computed here from :data:`agent_crew.cea.validator._P6_MATRIX`.

    ⛔Deliberately **not** conditioned on ``enforcing()``. Shadow/enforce decides
      whether a *receipt* verdict stops work; the #314 operator STOP is not a
      receipt verdict, and making it follow the CEA rollout switch would mean
      turning CEA off also turned the fleet pause off.
    """
    return RuntimeGateOutcome(
        point=point,
        verdict=runtime_state_verdict(runtime_state, point,
                                      already_claimed=already_claimed,
                                      already_dispatched=already_dispatched))

# ── re-admission: the paths that put a claimed row back in the queue ────────
#
# ADR §8 ("Recovery semantics"). These are NOT a sixth validation point: nothing
# here moves a task into an execution state, so P2's five gates stay five. What
# they decide is the question §8 asks instead — *may this row go round again on
# the receipt it already has?* — and the answer has exactly three shapes:
#
#   PROCEED   B unchanged and ``attempt < max_attempts``: reuse the receipt,
#             recording the re-admission as ``attempt + 1`` (§8 "Retry of the
#             same task"). The receipt goes back to ``HELD`` — P3's "not claimed;
#             task → HELD with reason" — which ``claim`` accepts, so the row is
#             live again without a second writer of ``QUEUED``.
#   RE_ADMIT  B drifted or the attempt budget is spent: the old receipt is
#             SUPERSEDED and the work needs a new admission. Nothing here invents
#             one — re-authorising is the engine's job, at the engine's entry.
#   HELD      P7: B′ could not be computed. Work already authorised is held,
#             never blocked and never silently reused against an unknown B′.
#
#: Every product path that returns a claimed row to ``pending``. A path not in
#: this table has no §8 answer, so :func:`gate_requeue` refuses to answer for it
#: rather than defaulting — an unregistered requeue is the bypass this registry
#: exists to make visible.
REQUEUE_CALL_SITES: dict[str, tuple[str, ...]] = {
    # path (the one queue method that owns the mutation) → product entry points
    "queue.requeue": ("server._requeue_orphans (startup: in_progress → pending)",
                      "server push/spawn failure rollback",
                      "cli.recover"),
    "queue.defer_push_delivery": ("server tmux push refused by the pane (G_DT backoff)",),
    "queue.reset_stale_to_pending": ("cli.recover --reset-stale (#155)",),
}
"""ADR §8 re-admission paths, registered so I2 can enumerate them.

``retry`` is deliberately absent: ``server._auto_retry_failed_task`` creates a
**new task** through ``enqueue`` (ingress ``retry.failed_task``), so it is an
I1 ingress with its own receipt, not a path back to pending on an old one.
"""


@dataclass(frozen=True)
class RequeueOutcome:
    """What a §8 re-admission path learned, and whether it may act on it."""
    path: str
    receipt_id: str
    outcome: ValidationOutcome
    reason: str
    attempt: Optional[int]
    changed_fields: tuple[str, ...]
    proceed: bool
    enforced: bool

    @property
    def reuse(self) -> bool:
        """May the row go round again on the receipt it already has?"""
        return self.outcome is ValidationOutcome.PROCEED

    def as_record(self) -> dict:
        return {"path": self.path, "outcome": self.outcome.value, "reason": self.reason,
                "receipt_id": self.receipt_id, "attempt": self.attempt,
                "changed_fields": list(self.changed_fields),
                "proceed": self.proceed, "enforced": self.enforced}


def gate_requeue(receipt: Optional[dict], *, path: str, current: CurrentInputs,
                 config: Optional[EngineConfig] = None) -> RequeueOutcome:
    """§8: may ``path`` return this row to the queue on its existing receipt?

    ⛔A missing receipt is not a pass. A row written before receipts existed has
      nothing to re-admit, so under enforcement it is refused (and the caller
      records a runtime event saying so); under ``shadow`` it is *reported* and
      the requeue happens, which is the count that says how many legacy rows a
      deployment still has to drain before it turns enforcement on.
    """
    if path not in REQUEUE_CALL_SITES:
        raise KeyError(f"{path!r} is not a registered §8 re-admission path; "
                       f"add it to REQUEUE_CALL_SITES before requeuing through it")
    enforce = enforcing(config)

    def answer(outcome: ValidationOutcome, reason: str, *, attempt: Optional[int] = None,
               changed: tuple[str, ...] = ()) -> RequeueOutcome:
        return RequeueOutcome(
            path=path, receipt_id=(receipt or {}).get("receipt_id", "") if isinstance(receipt, dict) else "",
            outcome=outcome, reason=reason, attempt=attempt, changed_fields=changed,
            proceed=(outcome is ValidationOutcome.PROCEED) or not enforce, enforced=enforce)

    if not isinstance(receipt, dict):
        return answer(ValidationOutcome.BLOCK,
                      "NO_RECEIPT: the row names no receipt, so there is nothing to re-admit (P2)")
    state = str(receipt.get("state") or "")
    if state in ("CONSUMED", "SUPERSEDED", "REVOKED"):
        return answer(ValidationOutcome.RE_ADMIT,
                      f"receipt state is {state}; terminal receipts never re-enter (P4) — "
                      f"the work needs a new admission")
    if current.binding is None or current.unavailable_inputs:
        missing = ", ".join(current.unavailable_inputs) or "binding"
        return answer(ValidationOutcome.HELD,
                      f"B′ could not be computed ({missing}); P7 holds work already authorised "
                      f"rather than re-admitting it against an unknown binding")
    changed = binding_drifted(receipt.get("binding") or {}, current.binding)
    if changed:
        return answer(ValidationOutcome.RE_ADMIT,
                      f"BINDING_DRIFT: {', '.join(changed)} changed since issue; the old receipt "
                      f"is SUPERSEDED and admission runs again (§8, P3)", changed=changed)
    attempt, max_attempts = int(receipt.get("attempt") or 1), int(receipt.get("max_attempts") or 1)
    if attempt >= max_attempts:
        return answer(ValidationOutcome.RE_ADMIT,
                      f"ATTEMPTS_EXHAUSTED: attempt {attempt} of {max_attempts}; §8 reuses the "
                      f"receipt only while attempt < max_attempts", attempt=attempt)
    return answer(ValidationOutcome.PROCEED,
                  f"RE-QUEUED: B unchanged and attempt {attempt + 1} of {max_attempts} remains (§8)",
                  attempt=attempt + 1, changed=changed)


CALL_SITES = {
    ValidationPoint.ENQUEUE: gate_enqueue,
    ValidationPoint.CLAIM: gate_claim,
    ValidationPoint.DISPATCH: gate_dispatch,
    ValidationPoint.EXECUTE_START: gate_execute_start,
    ValidationPoint.RESULT: gate_result,
}
"""Every :class:`ValidationPoint` has exactly one gate — checked by the I2 test."""


__all__ = ["CALL_SITES", "GateOutcome", "REQUEUE_CALL_SITES", "RequeueOutcome",
           "RuntimeGateOutcome", "VALIDATOR",
           "current_inputs", "enforcing", "recording",
           "gate_claim", "gate_dispatch", "gate_enqueue", "gate_execute_start", "gate_requeue",
           "gate_result", "gate_runtime_state"]
