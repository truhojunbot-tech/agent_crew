"""SEV-0 CEA s4f — how a refused admission leaves the process over a wire.

Contract: alfred ``sev0/e11-adr-draft`` @ ``6cbce565`` (Π P2, §7).

``AdmissionRefused`` is the validator's own answer (``queue.AdmissionRefused``
carries the :class:`~agent_crew.cea.callsites.GateOutcome`, not a re-phrasing of
it). Inside the process that is enough. At a transport boundary it is not: an
uncaught exception becomes a 500 with a traceback, and a 500 says "this server
is broken" when what actually happened is "this server refused you, on purpose,
and here is the receipt that says why".

⛔The status code is chosen from the *machine reason code*, never from the
prose. ``reason`` is "machine code + text" (``ValidationResult.reason``); the
text is for humans and may be reworded, so matching on it would make the wire
contract drift the next time somebody improves a sentence.

The four codes are the ones the s4f task froze, and each says something a
client can act on differently:

===== ======================================= ==========================
 code  meaning                                 what a client should do
===== ======================================= ==========================
 401   the caller was never authenticated      get credentials, retry
 403   the engine decided BLOCK                do not retry; read reason
 409   this intent is already admitted         reuse the existing task
 423   runtime state / budget holds it, *now*  retry when state clears
===== ======================================= ==========================

423 (Locked) covers HELD and HUMAN_GATE as well as the RUNTIME_* codes: all
three mean "not now" rather than "not ever", which is the distinction a retrying
client needs and the one 403 would destroy.
"""
from __future__ import annotations

#: Machine reason-code prefixes → status. Order matters: first match wins, so
#: the specific codes sit above the outcome-level fallback.
_BY_REASON: tuple[tuple[str, int], ...] = (
    ("UNAUTHENTICATED", 401),
    ("DUPLICATE_INTENT", 409),
    ("RUNTIME_", 423),
    ("BUDGET_EXHAUSTED", 423),
    ("HUMAN_GATE_PENDING", 423),
)

#: Outcome → status when no reason code above matched. ``HELD``/``HUMAN_GATE``
#: are deferrals (423); everything else that refused is a decision (403).
_BY_OUTCOME: dict[str, int] = {"HELD": 423, "HUMAN_GATE": 423}


def _code(reason: object) -> str:
    """The leading machine code of a ``reason``, upper-cased; '' if there is none."""
    if isinstance(reason, dict):                      # some points carry a dict
        reason = reason.get("code") or reason.get("reason") or ""
    text = str(reason or "").strip()
    return text.split(":", 1)[0].split(" ", 1)[0].upper()


def status_for(outcome: object, reason: object) -> int:
    """Map one refusal to its HTTP status. Never raises — a bad input is a 403."""
    code = _code(reason)
    for prefix, status in _BY_REASON:
        if code.startswith(prefix):
            return status
    return _BY_OUTCOME.get(str(getattr(outcome, "value", outcome) or "").upper(), 403)


def refusal_payload(exc) -> tuple[int, dict]:
    """``(status, body)`` for an :class:`~agent_crew.queue.AdmissionRefused`.

    The body always carries ``receipt_id`` when the refusal had one — that is
    the audit row a caller quotes back when they ask why, and P2 wrote it before
    this exception existed. ``receipt_id`` is ``None`` only when admission was
    refused before any receipt could be written.
    """
    outcome = getattr(exc, "outcome", None)
    reason = getattr(getattr(exc, "gate", None), "reason", None)
    if reason is None:
        reason = str(exc)
    return status_for(outcome, reason), {
        "error": "admission refused",
        "point": getattr(exc, "point", None),
        "outcome": str(getattr(outcome, "value", outcome)) if outcome else None,
        "reason": reason,
        "receipt_id": getattr(exc, "receipt_id", None),
    }
