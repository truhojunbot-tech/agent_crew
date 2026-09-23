"""J8 human gate — from snapshot decision records only (§2.3 J8, CXC-4).

Stricter than the engine's default :class:`~agent_crew.cea.engine.SnapshotHumanGate`
in one respect: a predicate saying ``GRANTED`` counts only if its
``decision_id`` is a decision record *in the same snapshot*. A GRANTED with an
id the snapshot does not carry is free text with a ticket-shaped name, so it
reads as ``PENDING``. No request field, env value or caller flag is consulted.
"""
from __future__ import annotations

from agent_crew.cea.engine import SnapshotHumanGate
from agent_crew.cea.intent import Intent
from agent_crew.cea.providers import PolicySnapshotRef
from agent_crew.cea.receipt import HumanGate, HumanGateState


class SnapshotRecordGate:
    def state(self, intent: Intent, snapshot: PolicySnapshotRef) -> HumanGate:
        gate = SnapshotHumanGate().state(intent, snapshot)
        if gate.state is HumanGateState.GRANTED:
            known = {r.decision_id for r in snapshot.decisions or ()}
            if gate.decision_id not in known:
                return HumanGate(HumanGateState.PENDING)
        return gate
