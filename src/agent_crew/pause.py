"""#311 — a generic runtime STOP that reaches the queue, not just the feeder.

A feeder-level STOP upstream prevented new dispatches being *handed to* this
runtime, and the runtime kept draining the tasks already in its queue. So STOP
looked effective from above while work continued below it. The gap is that
nothing in the execution path itself asked "am I allowed to start anything right
now" — the only guard lived in the thing that supplies work.

This module is that missing guard, and it is deliberately generic:

⛔No system-manager dependency, in either direction. The runtime owns a pause
  STATE and a pause DECISION; deciding *when* a fleet should stop belongs to
  whatever drives it. An external manager sets the state through the CLI/API and
  reads the status back. Agent Crew core imports nothing from any fleet.

Two scopes, both optional and both config-driven:

- **project** — a row in the project's own queue database, so it survives a
  restart exactly as the queue does.
- **global/runtime** — a JSON file whose path comes from
  ``AGENT_CREW_GLOBAL_PAUSE_FILE``. ⛔Env-driven with no default path baked in:
  a hardcoded shared location would be a deployment assumption, and a runtime
  that invents one cannot be packaged.

⛔Generation, not timestamps. Resume names the generation it believes is current
  and is REFUSED if a newer STOP has landed since — a resume command that raced
  a fresh incident must not silently reopen the gate. Time cannot decide this:
  two clocks and a retry are enough to make "later" meaningless, and the failure
  mode is resuming into an incident nobody has cleared.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Where a runtime-wide (cross-project) pause lives, when the deployment wants
#: one. No default: absent env means "this runtime has no global scope".
GLOBAL_PAUSE_FILE_ENV = "AGENT_CREW_GLOBAL_PAUSE_FILE"

#: Checked in this order, and the order is the point: when both scopes are
#: paused the WIDER incident is reported first, so an operator clearing a local
#: STOP is told there is a runtime-wide one behind it.
PAUSE_SCOPES = ("global", "project")


@dataclass(frozen=True)
class PauseState:
    """One scope's pause record.

    ``generation`` is monotonic per scope and increments on every activation.
    It is the only thing resume is allowed to key on.
    """

    paused: bool = False
    scope: str = "project"
    reason: str = ""
    source: str = ""
    incident_ref: str = ""
    generation: int = 0
    activated_at: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "paused": self.paused,
            "scope": self.scope,
            "reason": self.reason,
            "source": self.source,
            "incident_ref": self.incident_ref,
            "generation": self.generation,
            "activated_at": self.activated_at,
        }

    @staticmethod
    def from_dict(data: dict[str, Any] | None, *, scope: str = "project") -> "PauseState":
        data = data if isinstance(data, dict) else {}
        try:
            generation = int(data.get("generation") or 0)
        except (TypeError, ValueError):
            generation = 0
        activated = data.get("activated_at")
        try:
            activated = float(activated) if activated is not None else None
        except (TypeError, ValueError):
            activated = None
        return PauseState(
            paused=bool(data.get("paused")),
            scope=str(data.get("scope") or scope),
            reason=str(data.get("reason") or ""),
            source=str(data.get("source") or ""),
            incident_ref=str(data.get("incident_ref") or ""),
            generation=generation,
            activated_at=activated,
        )


@dataclass(frozen=True)
class PauseDecision:
    """Whether a transition may proceed, and the provenance of the answer.

    ⛔`allowed=False` always carries a reason and a scope. A blocked transition
      that cannot say which scope blocked it, why, or under what generation is
      indistinguishable from a bug, and an operator cannot clear it.
    """

    allowed: bool
    scope: str = ""
    reason: str = ""
    source: str = ""
    incident_ref: str = ""
    generation: int = 0
    transition: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "blocked_by_scope": self.scope,
            "reason": self.reason,
            "source": self.source,
            "incident_ref": self.incident_ref,
            "generation": self.generation,
            "transition": self.transition,
        }


#: Transitions this gate covers. Named so telemetry says which one was blocked
#: rather than only that something was.
CLAIM = "claim"
DISPATCH = "dispatch"
CASCADE = "cascade"
RECOVERY = "recovery"


def decide(states, transition: str = CLAIM) -> PauseDecision:
    """May ``transition`` proceed, given every scope's state?

    ⛔Any paused scope blocks. Scopes are not a precedence ladder where a
      project can un-pause itself out of a runtime-wide STOP — that would make
      the global scope advisory, which is the exact shape of the bug this fixes.
      The first paused scope in ``PAUSE_SCOPES`` order is reported, so a global
      incident is named ahead of a local one.
    """
    by_scope = {s.scope: s for s in states if s is not None}
    for scope in PAUSE_SCOPES:
        state = by_scope.get(scope)
        if state is not None and state.paused:
            return PauseDecision(
                allowed=False,
                scope=state.scope,
                reason=state.reason or "paused",
                source=state.source,
                incident_ref=state.incident_ref,
                generation=state.generation,
                transition=transition,
            )
    return PauseDecision(allowed=True, transition=transition)


def resume_is_stale(state: PauseState, generation) -> bool:
    """Is this resume about an older STOP than the one in force?

    ⛔A resume names the generation it believes it is clearing. If the scope has
      moved on — a second incident landed after the operator typed the command —
      the resume is about a STOP that is no longer the reason work is halted,
      and honouring it would reopen the gate on an uncleared incident.

    A resume that names no generation is stale by definition: it cannot prove it
    knows what it is clearing.
    """
    if generation is None:
        return True
    try:
        named = int(generation)
    except (TypeError, ValueError):
        return True
    return named < state.generation


class GlobalPauseFile:
    """Runtime-wide pause, stored wherever the deployment says (#311).

    ⛔Missing file means NOT paused, and an unreadable one means paused. The
      asymmetry is deliberate: "no global scope configured" is the normal case
      for a standalone install and must not halt it, while "a global STOP exists
      and I cannot read it" is exactly when guessing is unsafe.
    """

    def __init__(self, path: str = ""):
        self._path = path or os.getenv(GLOBAL_PAUSE_FILE_ENV, "") or ""

    @property
    def configured(self) -> bool:
        return bool(self._path)

    def read(self) -> PauseState:
        if not self._path:
            return PauseState(scope="global")
        if not os.path.exists(self._path):
            return PauseState(scope="global")
        try:
            with open(self._path, encoding="utf-8") as fh:
                return PauseState.from_dict(json.load(fh), scope="global")
        except Exception:  # noqa: BLE001
            logger.exception(
                f"GlobalPauseFile: {self._path!r} exists but could not be read — "
                f"treating the runtime as PAUSED. A global STOP that cannot be "
                f"read is not evidence that there is none (#311)."
            )
            return PauseState(
                paused=True, scope="global",
                reason="global pause file exists but is unreadable",
                source="agent_crew", generation=0, activated_at=None,
            )

    def write(self, state: PauseState) -> None:
        if not self._path:
            raise RuntimeError(
                f"no global pause file configured; set {GLOBAL_PAUSE_FILE_ENV}")
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{self._path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state.to_dict(), fh)
        os.replace(tmp, self._path)

    def activate(self, *, reason: str, source: str = "", incident_ref: str = "") -> PauseState:
        current = self.read()
        state = PauseState(
            paused=True, scope="global", reason=reason, source=source,
            incident_ref=incident_ref, generation=current.generation + 1,
            activated_at=time.time(),
        )
        self.write(state)
        return state

    def release(self, generation) -> tuple[bool, PauseState]:
        current = self.read()
        if not current.paused:
            return (True, current)
        if resume_is_stale(current, generation):
            return (False, current)
        state = PauseState(
            paused=False, scope="global", reason="", source="",
            incident_ref="", generation=current.generation, activated_at=None,
        )
        self.write(state)
        return (True, state)
