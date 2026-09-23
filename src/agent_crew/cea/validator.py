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

import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional, Protocol, runtime_checkable

from agent_crew.cea.receipt import DispatchNonce, Receipt
from agent_crew.cea.schema import validate_receipt


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
    OK = "PROCEED"          # alias — the task contract spells this ``ok``
    RE_ADMIT = "RE_ADMIT"
    READMIT = "RE_ADMIT"    # alias
    BLOCK = "BLOCK"
    HUMAN_GATE = "HUMAN_GATE"
    HELD = "HELD"


@dataclass(frozen=True)
class ValidationResult:
    point: ValidationPoint
    outcome: ValidationOutcome
    reason: str                                  # machine code + text
    receipt_id: str
    binding_now: Optional[dict] = None           # B′ as computed at this point (None if it could not be)
    nonce: Optional[DispatchNonce] = None        # minted at DISPATCH when outcome is PROCEED
    changed_fields: tuple[str, ...] = ()         # which B fields drifted (P3 table rows)


@runtime_checkable
class ReceiptValidator(Protocol):
    """T3 — the single validator implementation, invoked from five call sites.

    Every method takes ``current``: the validator does no I/O, so the current
    values it compares against are read by the caller (P1 input providers) and
    handed in. A protocol without it described an implementation that cannot
    check anything — the call sites in :mod:`agent_crew.cea.callsites` all pass it.
    """

    def validate_enqueue(self, receipt: Receipt, *, task_id: str,
                         current: Optional["CurrentInputs"] = None) -> ValidationResult:
        """P2 Enqueue: receipt exists, signature valid, ``decision=ALLOW`` (or
        ``REVIEW`` with the reviewer task created in the same transaction),
        ``task_id`` matches, binding current. Runs inside the caller's
        ``BEGIN IMMEDIATE``; on failure no row is written."""
        ...

    def validate_claim(self, receipt: Receipt, *, claimant: Optional[str] = None,
                       current: Optional["CurrentInputs"] = None) -> ValidationResult:
        """P2 Claim: signature; not REVOKED/CONSUMED; ``executor_binding`` matches
        the claimant's *asserted* identity (UNVERIFIED under P2a); binding
        current; runtime state permits claim (P6)."""
        ...

    def validate_dispatch(self, receipt: Receipt, *, attempt: Optional[int] = None,
                          current: Optional["CurrentInputs"] = None) -> ValidationResult:
        """P2 Dispatch: as claim, re-read; mints a fresh single-use dispatch
        nonce bound to ``(receipt_id, attempt)`` and returns it in ``nonce``."""
        ...

    def validate_execute_start(self, receipt: Receipt, *, nonce: Optional[str] = None,
                               current: Optional["CurrentInputs"] = None) -> ValidationResult:
        """P2 Execute start: as dispatch; the nonce is unused and matches.
        One-shot: pre-spawn. Pane: ``POST /tasks/{id}/start {nonce}`` → go/no-go."""
        ...

    def validate_result(self, receipt: Receipt, *, nonce: Optional[str] = None,
                        presenter: Optional[str] = None,
                        current: Optional["CurrentInputs"] = None) -> ValidationResult:
        """§2.2 / P2 last paragraph: ``/result`` accepted only with the dispatch
        nonce presented under ``executor_binding``; a second result on a terminal
        task is 409 (P4). Identity-dependent verdicts under UNVERIFIED binding
        never auto-cascade (P2a; CX-4h, CX-P2b)."""
        ...


# ═══════════════════════════════════════════════════════════════════════════
# The one implementation (P2: "one receipt validator implementation ... four
# call sites"; five here, because ``/result`` is the fifth state transition).
#
# It is a pure function: every current value it needs arrives in
# :class:`CurrentInputs`, so it can be unit-tested over the P3 / P6 / P7 tables
# without a database, and so it can be called from inside a ``BEGIN IMMEDIATE``
# transaction without doing its own I/O. It **never re-decides policy** (P1) —
# when the binding drifted it says ``RE_ADMIT`` and the engine re-runs.
# ═══════════════════════════════════════════════════════════════════════════

O18_MAX_RECEIPT_AGE_SECONDS = 24 * 60 * 60
"""``max_receipt_age`` (P3 last row, ADR §13 O18 recommendation 24 h). Exceeding
it is a **re-admission**, not a block: the intent is still valid, the authority
snapshot behind it is simply too old to trust without re-running the engine."""

O18_IMMEDIATE_INVALIDATION_FIELDS: tuple[str, ...] = (
    "policy_generation", "policy_hash", "source_decision_revs",
    "capability_registry", "matched_capability", "runtime_state",
    "runtime_state_epoch", "human_gate_state", "budget_class",
)
"""The B fields that invalidate a receipt the moment they change, independently
of ``max_receipt_age`` — i.e. the rows of the P3 outcome table. A receipt one
minute old is invalid if any of these moved; a receipt 23 h old with all of them
unchanged is still current. Age is the *backstop*, not the mechanism."""

O20_SNAPSHOT_MAX_AGE_SECONDS = 15 * 60
"""``snapshot_max_age`` while the SSOT is unreachable (P7 row 3, §13 O20
recommendation 15 min). Inside the window the last signed snapshot remains the
input ("bounded stale"); past it the runtime is fail-closed for new admission and
holds — never blocks — work that is already authorised."""


@dataclass(frozen=True)
class CurrentInputs:
    """Everything the validator compares against, read by the caller (P1 input
    providers). The validator does no I/O of its own.

    ``binding`` is B′ — the same shape as the receipt's ``binding`` object. It is
    ``None`` when B′ could not be computed at all (P7): the caller could not
    reach an input provider.
    """
    binding: Optional[dict] = None
    unavailable_inputs: tuple[str, ...] = ()      # P7: named providers that did not answer
    now: Optional[float] = None                   # epoch seconds; defaults to time.time()
    snapshot_available: bool = True               # P7 row 3: SSOT reachable?
    snapshot_age_seconds: Optional[float] = None  # age of the last signed snapshot (O20)
    epoch_passed_non_active: bool = False         # P3: epoch advanced *through* a non-ACTIVE state
    claimant: Optional[str] = None                # asserted identity at CLAIM (P2a: asserted, not proven)
    presenter: Optional[str] = None               # asserted identity at EXECUTE_START / RESULT
    presented_nonce: Optional[str] = None
    nonce_unused: Optional[bool] = None           # from the single-use nonce table (store.nonce_row)
    already_claimed: bool = False                 # P6 DRAINING: dispatch only what was already claimed
    already_dispatched: bool = False              # P6 DRAINING: execute only what was already dispatched
    max_receipt_age_seconds: float = O18_MAX_RECEIPT_AGE_SECONDS
    snapshot_max_age_seconds: float = O20_SNAPSHOT_MAX_AGE_SECONDS
    # ── verifier evidence (P2a: VERIFIED is the broker's/engine's word, not the
    #    caller's). The receipt is caller-controlled data at every one of the five
    #    points, so its own ``signature.status`` and ``*_status`` fields are
    #    claims, not findings. These two carry what a *trusted verifier* produced.
    signature_verification: Optional[bool] = None  # engine.verify()/broker result; None = nobody checked
    attested_identities: tuple[str, ...] = ()     # claims a verifier proved:
                                                  # "executor_binding", "caller_identity"


# P6 enforcement matrix, verbatim. Value is the outcome when the *current*
# runtime state is the row and the check point is the column. ``"claimed"`` and
# ``"dispatched"`` are conditional PROCEEDs resolved against CurrentInputs.
_P6_MATRIX: dict[str, dict[str, str]] = {
    "ACTIVE":      {"enqueue": "ok",    "claim": "ok",   "dispatch": "ok",        "execute_start": "ok",           "result": "ok"},
    "DRAINING":    {"enqueue": "BLOCK", "claim": "HELD", "dispatch": "claimed",   "execute_start": "dispatched",   "result": "ok"},
    "QUARANTINED": {"enqueue": "BLOCK", "claim": "BLOCK", "dispatch": "BLOCK",    "execute_start": "BLOCK",        "result": "flagged"},
    "STOPPED":     {"enqueue": "BLOCK", "claim": "BLOCK", "dispatch": "BLOCK",    "execute_start": "BLOCK",        "result": "flagged"},
}

# Which receipt lifecycle states may appear at each point (§3 ``state``).
_POINT_STATES: dict[str, tuple[str, ...]] = {
    "enqueue": ("ISSUED",),
    "claim": ("QUEUED", "HELD"),
    "dispatch": ("CLAIMED",),
    "execute_start": ("CLAIMED", "RUNNING"),
    "result": ("RUNNING", "CLAIMED"),
}

_TERMINAL_STATES = ("CONSUMED", "SUPERSEDED", "REVOKED")


def _result(point: ValidationPoint, outcome: ValidationOutcome, code: str, text: str,
            *, binding_now: Optional[dict] = None, receipt_id: str = "",
            changed: tuple[str, ...] = ()) -> ValidationResult:
    return ValidationResult(point=point, outcome=outcome, reason=f"{code}: {text}",
                            receipt_id=receipt_id, binding_now=binding_now, changed_fields=changed)


def _gate_state(value) -> str:
    """``human_gate_state`` is either a string enum or ``{state: GRANTED, decision_id}``."""
    if isinstance(value, dict):
        return str(value.get("state", ""))
    return str(value or "")


def _identity_dependent(receipt: dict) -> bool:
    """Does this receipt's decision depend on caller or executor identity (P2a)?

    P2a: "the engine may still issue ALLOW only for decisions whose review/test
    requirement **and** human-gate state do not depend on caller or executor
    identity". Concretely, a receipt is identity-dependent when it names a
    required reviewer or tester (role separation is an identity claim), when a
    human gate is involved at all, or when a reuse approval was recorded without
    a verified approver (§6.2).
    """
    if receipt.get("required_reviewer") or receipt.get("required_tester"):
        return True
    if _gate_state(receipt.get("human_gate_state")) != "NOT_REQUIRED":
        return True
    reuse = receipt.get("reuse")
    if isinstance(reuse, dict) and reuse.get("decision") in ("REUSE", "EXTEND") \
            and not reuse.get("approver_identity_verified"):
        return True
    return False


ATTESTABLE_IDENTITIES = ("executor_binding", "caller_identity")
"""The identity claims a verifier can attest to, each naming the receipt field
minus its ``_status`` suffix."""


def _verified_signature(receipt: dict, cur: "CurrentInputs") -> bool:
    """Is the signature VERIFIED *as a finding*, not as a claim?

    Both have to agree: the receipt says VERIFIED **and** a verifier the runtime
    trusts (``engine.verify()``, or a broker attestation) said so in
    ``CurrentInputs``. Either one alone is a caller assertion about itself.
    """
    claimed = (receipt.get("signature") or {}).get("status") == "VERIFIED"
    return claimed and cur.signature_verification is True


def _verified_identity(receipt: dict, cur: "CurrentInputs", field: str) -> bool:
    """P2a: ``VERIFIED`` on an identity binding is reserved for broker/engine.

    A receipt asserting ``executor_binding_status: VERIFIED`` proves nothing —
    the forged receipt in the review of 10153bf asserted all three. The status
    counts only when a verifier attested to that same field.
    """
    return receipt.get(f"{field}_status") == "VERIFIED" and field in tuple(cur.attested_identities)


def _parse_rfc3339(value: str) -> Optional[float]:
    from datetime import datetime, timezone
    try:
        text = value.replace("Z", "+00:00")
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() \
            if datetime.fromisoformat(text).tzinfo is None else datetime.fromisoformat(text).timestamp()
    except Exception:
        return None


def _revs(binding: dict) -> dict:
    out = {}
    for rev in binding.get("source_decision_revs") or []:
        if isinstance(rev, dict):
            out[str(rev.get("decision_id"))] = rev.get("body_hash")
    return out


def _drifted_fields(was: dict, now: dict) -> tuple[str, ...]:
    """Which of the P3 / O18 fields moved between B and B′."""
    changed: list[str] = []
    for field_name in ("policy_generation", "policy_hash"):
        if was.get(field_name) != now.get(field_name):
            changed.append(field_name)
    if _revs(was) != _revs(now):
        changed.append("source_decision_revs")
    was_reg, now_reg = was.get("capability_registry") or {}, now.get("capability_registry") or {}
    # ⛔Both fields, not just the generation. Registries in the field publish a
    #   dated generation the frozen schema types as integer|null, so the engine
    #   folds it into ``hash`` and leaves ``generation`` null — comparing only
    #   the generation would then make registry drift permanently invisible,
    #   which is the opposite of what this check is for.
    if was_reg.get("generation") != now_reg.get("generation") or was_reg.get("hash") != now_reg.get("hash"):
        changed.append("capability_registry")
    was_cap, now_cap = was.get("matched_capability") or {}, now.get("matched_capability") or {}
    if was_cap.get("owner") != now_cap.get("owner") or was_cap.get("id") != now_cap.get("id"):
        changed.append("matched_capability")
    if was.get("runtime_state") != now.get("runtime_state"):
        changed.append("runtime_state")
    if was.get("runtime_state_epoch") != now.get("runtime_state_epoch"):
        changed.append("runtime_state_epoch")
    if _gate_state(was.get("human_gate_state")) != _gate_state(now.get("human_gate_state")):
        changed.append("human_gate_state")
    if was.get("budget_class") != now.get("budget_class"):
        changed.append("budget_class")
    return tuple(changed)


def validate(receipt, point, current: Optional[CurrentInputs] = None) -> ValidationResult:
    """The single receipt check, at one of the five points (P2, P3, P6, P7).

    ``receipt`` is the canonical receipt **dict** — the wire/stored form the
    frozen schema describes — because that is what crosses the alfred↔agent_crew
    boundary and what the store holds. ``point`` is a :class:`ValidationPoint`
    (or its string value). Returns a :class:`ValidationResult` whose ``outcome``
    is one of ``PROCEED`` (``OK``), ``HELD``, ``BLOCK``, ``HUMAN_GATE``,
    ``RE_ADMIT`` (``READMIT``).

    Order of judgement, and why:

    1. **Schema** — a receipt that is not the contract is not a receipt.
    2. **Lifecycle state** — terminal receipts never re-enter (P4 replay refusal).
    3. **Signature / P2a** — an unverified signature is admissible only where the
       ADR says it is; an identity-dependent ALLOW under an unverified binding is
       exactly what P2a forbids. "Verified" means *a verifier said so*
       (``CurrentInputs.signature_verification`` / ``attested_identities``); the
       receipt's own status fields are only the caller's claim about itself.
    4. **Verdict** — BLOCK/HUMAN_GATE receipts do not authorise anything.
    5. **Nonce** — reuse is a tamper signal and outranks every deferrable outcome.
    6. **P7** — if B′ could not be computed, fail closed at admission and hold
       (never block) work that was already authorised.
    7. **P6** — the runtime-state matrix for this point.
    8. **P3** — binding drift: block, human-gate, re-admit or hold, in that order.

    At ``RESULT`` drift never blocks: P3's "already running when B changes" rule
    says the result is accepted and marked stale, because killing in-flight work
    is an owner decision, and containment is on the successors instead.
    """
    point = ValidationPoint(point) if not isinstance(point, ValidationPoint) else point
    cur = current or CurrentInputs()
    now = cur.now if cur.now is not None else time.time()
    rid = receipt.get("receipt_id", "") if isinstance(receipt, dict) else ""
    B_now = cur.binding

    # 1. schema — the frozen contract, byte-identical to alfred's copy
    if not isinstance(receipt, dict):
        return _result(point, ValidationOutcome.BLOCK, "RECEIPT_SCHEMA_INVALID",
                       f"receipt is {type(receipt).__name__}, not an object")
    errors = validate_receipt(receipt)
    if errors:
        return _result(point, ValidationOutcome.BLOCK, "RECEIPT_SCHEMA_INVALID",
                       f"{errors[0]} ({len(errors)} error(s))", receipt_id=rid)

    state = receipt["state"]

    # 2. lifecycle
    if state in _TERMINAL_STATES:
        code = {"CONSUMED": "RECEIPT_CONSUMED", "SUPERSEDED": "RECEIPT_SUPERSEDED",
                "REVOKED": "RECEIPT_REVOKED"}[state]
        return _result(point, ValidationOutcome.BLOCK, code,
                       f"receipt state is {state}; terminal receipts never re-enter (P4)", receipt_id=rid)
    allowed_states = _POINT_STATES[point.value]
    if state not in allowed_states:
        if state == "HELD":
            return _result(point, ValidationOutcome.HELD, "RECEIPT_HELD",
                           f"receipt is HELD; it returns to the engine, not to {point.value}", receipt_id=rid)
        return _result(point, ValidationOutcome.BLOCK, "RECEIPT_STATE_INVALID",
                       f"state {state} is not one of {allowed_states} at {point.value}", receipt_id=rid)

    # 3. signature status and the P2a rules.
    #
    # ⛔The receipt is caller-controlled data at all five points. Its own
    #   ``signature.status`` / ``*_status`` fields are *claims*; what counts is
    #   what a trusted verifier produced, which arrives in CurrentInputs. Before
    #   this, a receipt forged with every status set to VERIFIED, an arbitrary
    #   signature value, required_reviewer=codex and decision=ALLOW returned
    #   PROCEED — P2a was being enforced against the attacker's own answer
    #   (codex review of 10153bf, P1 #4).
    sig_status = (receipt.get("signature") or {}).get("status")
    if not _verified_signature(receipt, cur):
        if sig_status == "VERIFIED":
            return _result(point, ValidationOutcome.BLOCK, "SIGNATURE_UNVERIFIED_AT_BOUNDARY",
                           "signature.status says VERIFIED but no verifier did; P2a reserves "
                           "VERIFIED for the broker/engine, so the claim is treated as UNVERIFIED "
                           "— and an UNVERIFIED receipt must carry a downgrade_reason (§3)",
                           receipt_id=rid)
        if not receipt.get("downgrade_reason"):
            return _result(point, ValidationOutcome.BLOCK, "RECEIPT_UNSIGNED",
                           "signature.status is UNVERIFIED with no downgrade_reason; §3 integrity "
                           "requires the receipt to state why it is unverified", receipt_id=rid)
    unverified_identity = not (_verified_identity(receipt, cur, "executor_binding")
                               and _verified_identity(receipt, cur, "caller_identity"))
    if unverified_identity and receipt.get("decision") == "ALLOW" and _identity_dependent(receipt):
        return _result(point, ValidationOutcome.BLOCK, "IDENTITY_DEPENDENT_ALLOW_UNVERIFIED",
                       "P2a: an identity-dependent decision may not be ALLOW while "
                       "executor/caller binding is UNVERIFIED — it must be REVIEW or HUMAN_GATE",
                       receipt_id=rid)

    # 4. verdict
    decision = receipt["decision"]
    if decision == "BLOCK":
        return _result(point, ValidationOutcome.BLOCK, "DECISION_BLOCK",
                       "receipt decision is BLOCK", receipt_id=rid)
    if decision == "HUMAN_GATE":
        return _result(point, ValidationOutcome.HUMAN_GATE, "DECISION_HUMAN_GATE",
                       "receipt decision is HUMAN_GATE; nothing runs until the owner grants it",
                       receipt_id=rid)
    if decision == "REVIEW" and point is ValidationPoint.ENQUEUE and not receipt.get("required_reviewer"):
        return _result(point, ValidationOutcome.BLOCK, "REVIEW_WITHOUT_REVIEWER",
                       "P2 enqueue: a REVIEW receipt admits only with the reviewer named and the "
                       "reviewer task created in the same transaction", receipt_id=rid)

    # 5. nonce — single-use, bound to (receipt_id, attempt)
    if point in (ValidationPoint.EXECUTE_START, ValidationPoint.RESULT):
        nonce = cur.presented_nonce
        if not nonce:
            return _result(point, ValidationOutcome.BLOCK, "NONCE_MISSING",
                           f"{point.value} requires the dispatch nonce", receipt_id=rid)
        minted = {n.get("nonce"): n for n in receipt.get("dispatch_nonces") or []}
        if nonce not in minted:
            return _result(point, ValidationOutcome.BLOCK, "NONCE_UNKNOWN",
                           "presented nonce is not bound to this receipt", receipt_id=rid)
        if minted[nonce].get("attempt") != receipt.get("attempt"):
            return _result(point, ValidationOutcome.BLOCK, "NONCE_WRONG_ATTEMPT",
                           "nonce belongs to a different attempt", receipt_id=rid)
        if point is ValidationPoint.EXECUTE_START and cur.nonce_unused is False:
            return _result(point, ValidationOutcome.BLOCK, "NONCE_REUSED",
                           "dispatch nonce was already spent (P4: single-use)", receipt_id=rid)
    presented = cur.claimant if point is ValidationPoint.CLAIM else cur.presenter
    if presented is not None and receipt.get("executor_binding") \
            and presented != receipt["executor_binding"]:
        # P2a: this compares *asserted* identities. It is tamper-evident, and it
        # counts as enforcement only once executor_binding_status is VERIFIED.
        return _result(point, ValidationOutcome.BLOCK, "EXECUTOR_BINDING_MISMATCH",
                       f"{presented!r} is not the bound executor {receipt['executor_binding']!r}",
                       receipt_id=rid)

    # 6. P7 — B′ not computable, or the snapshot is past its bounded-stale window
    fail_closed_point = point is ValidationPoint.ENQUEUE
    if B_now is None or cur.unavailable_inputs:
        missing = ", ".join(cur.unavailable_inputs) or "binding"
        if point is ValidationPoint.RESULT:
            return _result(point, ValidationOutcome.PROCEED, "RESULT_ACCEPTED_INPUTS_UNAVAILABLE",
                           f"in-flight work is never killed for an unavailable input ({missing})",
                           receipt_id=rid)
        outcome = ValidationOutcome.BLOCK if fail_closed_point else ValidationOutcome.HELD
        return _result(point, outcome, "INPUTS_UNAVAILABLE",
                       f"B′ could not be computed ({missing}); P7 fails closed at admission and "
                       f"holds authorised work", receipt_id=rid, binding_now=B_now)
    if not cur.snapshot_available:
        age = cur.snapshot_age_seconds
        if age is None or age > cur.snapshot_max_age_seconds:
            if point is ValidationPoint.RESULT:
                return _result(point, ValidationOutcome.PROCEED, "RESULT_ACCEPTED_SNAPSHOT_STALE",
                               "SSOT unreachable past snapshot_max_age; the result is still accepted",
                               receipt_id=rid, binding_now=B_now)
            outcome = ValidationOutcome.BLOCK if fail_closed_point else ValidationOutcome.HELD
            return _result(point, outcome, "SNAPSHOT_STALE",
                           f"SSOT unreachable and the last signed snapshot is older than "
                           f"snapshot_max_age ({cur.snapshot_max_age_seconds:.0f}s, O20)",
                           receipt_id=rid, binding_now=B_now)
        # inside the window: the last signed snapshot remains the input (bounded stale)

    # 7. P6 enforcement matrix on the *current* runtime state
    runtime_state = B_now.get("runtime_state", "STOPPED")
    cell = _P6_MATRIX.get(runtime_state, _P6_MATRIX["STOPPED"])[point.value]
    if cell == "BLOCK":
        return _result(point, ValidationOutcome.BLOCK, "RUNTIME_STATE_FORBIDS",
                       f"runtime state {runtime_state} forbids {point.value} (P6)",
                       receipt_id=rid, binding_now=B_now)
    if cell == "HELD":
        return _result(point, ValidationOutcome.HELD, "RUNTIME_DRAINING",
                       "runtime is DRAINING; queued work stays HELD (P6)",
                       receipt_id=rid, binding_now=B_now)
    if cell == "claimed" and not cur.already_claimed:
        return _result(point, ValidationOutcome.HELD, "RUNTIME_DRAINING",
                       "runtime is DRAINING; only already-claimed work may dispatch (P6)",
                       receipt_id=rid, binding_now=B_now)
    if cell == "dispatched" and not cur.already_dispatched:
        return _result(point, ValidationOutcome.HELD, "RUNTIME_DRAINING",
                       "runtime is DRAINING; only already-dispatched work may start (P6)",
                       receipt_id=rid, binding_now=B_now)

    # 8. P3 binding drift
    B_was = receipt.get("binding") or {}
    changed = _drifted_fields(B_was, B_now)
    if point is ValidationPoint.RESULT:
        if changed:
            return _result(point, ValidationOutcome.PROCEED, "STALE_RECEIPT",
                           "binding drifted while the task ran; the result is accepted and flagged "
                           "stale, and every successor is re-authorised under the current B (P3)",
                           receipt_id=rid, binding_now=B_now, changed=changed)
        if cell == "flagged":
            return _result(point, ValidationOutcome.PROCEED, "RESULT_ACCEPTED_NO_SUCCESSORS",
                           f"runtime state {runtime_state}: the result is recorded, successors are "
                           f"suppressed (P6)", receipt_id=rid, binding_now=B_now)
        return _result(point, ValidationOutcome.PROCEED, "OK", "result accepted",
                       receipt_id=rid, binding_now=B_now)

    if "runtime_state_epoch" in changed or cur.epoch_passed_non_active:
        return _result(point, ValidationOutcome.BLOCK, "RUNTIME_EPOCH_ADVANCED",
                       "the runtime state epoch advanced through a non-ACTIVE state; the task is "
                       "HELD and returns to the engine — it never resumes on the old receipt (P3)",
                       receipt_id=rid, binding_now=B_now, changed=changed)
    gate_now = _gate_state(B_now.get("human_gate_state"))
    gate_was = _gate_state(B_was.get("human_gate_state"))
    if gate_was == "GRANTED" and gate_now != "GRANTED":
        return _result(point, ValidationOutcome.BLOCK, "HUMAN_GATE_REVOKED",
                       f"human gate went GRANTED → {gate_now or 'absent'} (P3)",
                       receipt_id=rid, binding_now=B_now, changed=changed)
    if gate_now == "PENDING":
        return _result(point, ValidationOutcome.HUMAN_GATE, "HUMAN_GATE_PENDING",
                       "human gate is still PENDING; the task is held and nothing runs (P3)",
                       receipt_id=rid, binding_now=B_now, changed=changed)
    if gate_now == "DENIED":
        return _result(point, ValidationOutcome.BLOCK, "HUMAN_GATE_DENIED",
                       "human gate is DENIED (P3)", receipt_id=rid, binding_now=B_now, changed=changed)

    readmit = [f for f in ("policy_generation", "policy_hash", "source_decision_revs",
                           "capability_registry", "matched_capability") if f in changed]
    if readmit:
        return _result(point, ValidationOutcome.RE_ADMIT, "BINDING_DRIFT",
                       f"{', '.join(readmit)} changed since issue; the engine re-runs on the same "
                       f"intent and the old receipt becomes SUPERSEDED (P3)",
                       receipt_id=rid, binding_now=B_now, changed=changed)

    issued = _parse_rfc3339(receipt.get("issued_at", ""))
    if issued is not None and (now - issued) > cur.max_receipt_age_seconds:
        return _result(point, ValidationOutcome.RE_ADMIT, "RECEIPT_TOO_OLD",
                       f"receipt is older than max_receipt_age "
                       f"({cur.max_receipt_age_seconds:.0f}s, O18); re-admission (P3)",
                       receipt_id=rid, binding_now=B_now, changed=changed)

    if B_now.get("budget_class") == "EXHAUSTED":
        return _result(point, ValidationOutcome.HELD, "BUDGET",
                       "provider budget is EXHAUSTED; HELD until the budget input clears (P3)",
                       receipt_id=rid, binding_now=B_now, changed=changed)

    return _result(point, ValidationOutcome.PROCEED, "OK",
                   f"receipt valid at {point.value}", receipt_id=rid, binding_now=B_now,
                   changed=changed)


class ContractReceiptValidator:
    """The :class:`ReceiptValidator` protocol, implemented by the one :func:`validate`.

    Provided so the five call sites in step 2 depend on an object with named
    methods (P2 "four call sites, one implementation") while every rule stays in
    a single pure function.
    """

    def validate_enqueue(self, receipt, *, task_id: str,
                         current: Optional[CurrentInputs] = None) -> ValidationResult:
        if isinstance(receipt, dict) and receipt.get("task_id") != task_id:
            return _result(ValidationPoint.ENQUEUE, ValidationOutcome.BLOCK, "TASK_ID_MISMATCH",
                           f"receipt is for task {receipt.get('task_id')!r}, not {task_id!r}",
                           receipt_id=receipt.get("receipt_id", ""))
        return validate(receipt, ValidationPoint.ENQUEUE, current)

    def validate_claim(self, receipt, *, claimant: Optional[str] = None,
                       current: Optional[CurrentInputs] = None) -> ValidationResult:
        cur = current or CurrentInputs()
        if claimant is not None:
            cur = replace(cur, claimant=claimant)
        return validate(receipt, ValidationPoint.CLAIM, cur)

    def validate_dispatch(self, receipt, *, attempt: Optional[int] = None,
                          current: Optional[CurrentInputs] = None) -> ValidationResult:
        if attempt is not None and isinstance(receipt, dict) and receipt.get("attempt") != attempt:
            return _result(ValidationPoint.DISPATCH, ValidationOutcome.BLOCK, "ATTEMPT_MISMATCH",
                           f"receipt is at attempt {receipt.get('attempt')}, dispatch asked for {attempt}",
                           receipt_id=receipt.get("receipt_id", ""))
        return validate(receipt, ValidationPoint.DISPATCH, current)

    def validate_execute_start(self, receipt, *, nonce: Optional[str] = None,
                               current: Optional[CurrentInputs] = None) -> ValidationResult:
        cur = current or CurrentInputs()
        if nonce is not None:
            cur = replace(cur, presented_nonce=nonce)
        return validate(receipt, ValidationPoint.EXECUTE_START, cur)

    def validate_result(self, receipt, *, nonce: Optional[str] = None,
                        presenter: Optional[str] = None,
                        current: Optional[CurrentInputs] = None) -> ValidationResult:
        cur = current or CurrentInputs()
        if nonce is not None:
            cur = replace(cur, presented_nonce=nonce)
        if presenter is not None:
            cur = replace(cur, presenter=presenter)
        return validate(receipt, ValidationPoint.RESULT, cur)
