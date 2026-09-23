"""P2 receipt validator — one implementation, five call points (ADR Π P2, P3, P6, §11.2 T3).

"Without a valid receipt there is no enqueue, no claim, no dispatch, no
execute." The validator checks a receipt and **never re-decides policy** (P1);
when the binding ``B`` drifted it returns ``RE_ADMIT`` and the engine re-runs.

Call points, with their agent_crew anchors at ``b574308`` (see
``docs/sev0/cea-fold-plan.md`` §2):

=============  =====================================================================
point          where
=============  =====================================================================
ENQUEUE        ``queue.enqueue`` inside the same ``BEGIN IMMEDIATE`` that reads
               ``runtime_stop`` (``queue.py:1034-1038``)
CLAIM          ``queue.dequeue`` critical section (``queue.py:1326-1330``)
DISPATCH       the push/spawn path; P2 names "where ``_suppressed`` is computed"
               (``queue.py:1520-1522`` in ``submit_result`` for cascades;
               ``server.py:2781/2877`` push_fn, ``server.py:3285`` ``_dispatch_task``)
EXECUTE_START  one-shot: immediately before spawn; pane: ``POST /tasks/{id}/start {nonce}``
RESULT         ``POST /tasks/{id}/result`` (``server.py:4888``) — a state transition,
               accepted only with the single-use dispatch nonce under ``executor_binding``
=============  =====================================================================
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol, runtime_checkable

from agent_crew.cea.receipt import Binding, DispatchNonce, Receipt


class ValidationPoint(str, Enum):
    ENQUEUE = "enqueue"
    CLAIM = "claim"
    DISPATCH = "dispatch"
    EXECUTE_START = "execute_start"
    RESULT = "result"


class ValidationOutcome(str, Enum):
    """P3 outcome table, plus ``PROCEED`` (binding unchanged, checks pass).

    * ``RE_ADMIT`` — policy/decision/registry drift or ``max_receipt_age`` (O18):
      engine re-runs on the same intent, old receipt → SUPERSEDED.
    * ``BLOCK`` — runtime state forbids (P6), human gate revoked/denied,
      receipt REVOKED/CONSUMED, nonce reused, task_id/binding mismatch.
    * ``HUMAN_GATE`` — gate still PENDING: held, nothing runs.
    * ``HELD`` — deferred: budget EXHAUSTED, or ``B′`` needs an unavailable
      provider (P7); returns to the engine when the input clears.
    """
    PROCEED = "PROCEED"
    RE_ADMIT = "RE_ADMIT"
    BLOCK = "BLOCK"
    HUMAN_GATE = "HUMAN_GATE"
    HELD = "HELD"


@dataclass(frozen=True)
class ValidationResult:
    point: ValidationPoint
    outcome: ValidationOutcome
    reason: str                                  # machine code + text
    receipt_id: str
    binding_now: Optional[Binding] = None        # B′ as computed at this point (None if it could not be)
    nonce: Optional[DispatchNonce] = None        # minted at DISPATCH when outcome is PROCEED
    changed_fields: tuple[str, ...] = ()         # which B fields drifted (P3 table rows)


@runtime_checkable
class ReceiptValidator(Protocol):
    """T3 — the single validator implementation, invoked from five call sites."""

    def validate_enqueue(self, receipt: Receipt, *, task_id: str) -> ValidationResult:
        """P2 Enqueue: receipt exists, signature valid, ``decision=ALLOW`` (or
        ``REVIEW`` with the reviewer task created in the same transaction),
        ``task_id`` matches, binding current. Runs inside the caller's
        ``BEGIN IMMEDIATE``; on failure no row is written."""
        ...

    def validate_claim(self, receipt: Receipt, *, claimant: str) -> ValidationResult:
        """P2 Claim: signature; not REVOKED/CONSUMED; ``executor_binding`` matches
        the claimant's *asserted* identity (UNVERIFIED under P2a); binding
        current; runtime state permits claim (P6)."""
        ...

    def validate_dispatch(self, receipt: Receipt, *, attempt: int) -> ValidationResult:
        """P2 Dispatch: as claim, re-read; mints a fresh single-use dispatch
        nonce bound to ``(receipt_id, attempt)`` and returns it in ``nonce``."""
        ...

    def validate_execute_start(self, receipt: Receipt, *, nonce: str) -> ValidationResult:
        """P2 Execute start: as dispatch; the nonce is unused and matches.
        One-shot: pre-spawn. Pane: ``POST /tasks/{id}/start {nonce}`` → go/no-go."""
        ...

    def validate_result(self, receipt: Receipt, *, nonce: str, presenter: str) -> ValidationResult:
        """§2.2 / P2 last paragraph: ``/result`` accepted only with the dispatch
        nonce presented under ``executor_binding``; a second result on a terminal
        task is 409 (P4). Identity-dependent verdicts under UNVERIFIED binding
        never auto-cascade (P2a; CX-4h, CX-P2b)."""
        ...
