"""P6 runtime state — the generalised #314 ``runtime_stop`` row (ADR Π P6, §4).

Types only. The store itself (``paused: bool`` → ``state`` + ``epoch`` +
``reason`` + ``decision_id``, plus an append-only ``runtime_state_events``
table) is P6 work that starts after freeze; see ``docs/sev0/cea-fold-plan.md``
for the ``queue.py`` anchors it replaces.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class RuntimeState(str, Enum):
    """P6 ``state ∈ {ACTIVE, DRAINING, QUARANTINED, STOPPED}``.

    Enforcement matrix (P6, checked by the validator at every P2 point):

    ============  =======  =====  ========  =============  ==========================
    state         enqueue  claim  dispatch  execute start  /result
    ============  =======  =====  ========  =============  ==========================
    ACTIVE        engine   yes    yes       yes            yes
    DRAINING      BLOCK    no     claimed   dispatched     yes
    QUARANTINED   BLOCK    no     no        no-go          accepted, flagged, no successors
    STOPPED       BLOCK    no     no        no             per #314 (persist, suppress)
    ============  =======  =====  ========  =============  ==========================

    Anyone authenticated may *tighten*; only the owner may *loosen* out of
    QUARANTINED / STOPPED (P6 transitions table). A state that is not in the
    runtime row does not exist (P6 "doc-only states are forbidden"; CX-4j).
    """
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    QUARANTINED = "QUARANTINED"
    STOPPED = "STOPPED"

    @property
    def tightness(self) -> int:
        """Order used by "the more restrictive value wins" (P6, CX-P6b)."""
        return _TIGHTNESS[self]


_TIGHTNESS = {
    RuntimeState.ACTIVE: 0,
    RuntimeState.DRAINING: 1,
    RuntimeState.QUARANTINED: 2,
    RuntimeState.STOPPED: 3,
}


@dataclass(frozen=True)
class RuntimeStateSnapshot:
    """One read of the P6 row, as an input provider returns it.

    ``epoch`` increments on every transition (today: #314 ``runtime_stop.epoch``,
    monotonic across pause/resume). The P3 binding carries both ``state`` and
    ``epoch`` so an epoch that advanced through a non-ACTIVE state is detected
    even if the state is ACTIVE again.
    """
    state: RuntimeState
    epoch: int
    reason: Optional[str] = None
    decision_id: Optional[str] = None     # T0 record for owner-only transitions (P6)
    updated_at: Optional[float] = None
    read_failed: bool = False             # P7: unreadable ⇒ treated as STOPPED
