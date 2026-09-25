"""J-memory — the engine consults L2 and L3 before deciding (owner 11385, ADR §6, P1, P7).

No new engine branch: memory reaches the decision through the engine's existing
provider interfaces, so there is still exactly one decider (P1).

* **L2** — E4 capability matches (the only matcher) and the snapshot's standing /
  superseding decision records.
* **L3** — incident memory (``tools/admission_inputs.py`` ``incident_memory``
  block), entries with ``match.confidence == "HIGH"`` only. Low-confidence
  keyword hits are recorded in nothing and tighten nothing.

Two wrappers carry it into the engine:

:class:`MemoryCapabilities` (J4/J5) — a HIGH ``duplicate_work`` entry with a
``reuse_target`` becomes a :class:`MatchedCapability`, so the receipt records
``reuse.decision = REUSE`` and ``matched_capability = <reuse_target>``.

:class:`MemoryGate` (J8) — **tighten-only**. A HIGH entry whose
``recorded_disposition`` is ``BLOCK`` (counterexample / dangerous path) makes the
gate ``DENIED`` (engine: BLOCK); ``REUSE`` or ``REVIEW`` (duplicate work /
rejected approach) makes it ``PENDING`` (engine: HUMAN_GATE). Either way no new
task is admitted. The only way past a memory entry is a decision record *in the
signed snapshot* whose ``supersedes`` names the entry id (L2 standing rule), and
then the snapshot's own gate applies. Memory can never produce ``GRANTED``.

Fail direction (P7): if L3 did not answer, :class:`MemoryGate` raises, the engine
records ``human_gate`` as an unavailable input and BLOCKs. A known duplicate is
therefore never admitted because memory happened to be down.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from agent_crew.cea.input_providers.admission_inputs import AdmissionInputsClient, InputUnavailable
from agent_crew.cea.intent import Intent
from agent_crew.cea.providers import CapabilityLookup, PolicySnapshotRef
from agent_crew.cea.receipt import HumanGate, HumanGateState, MatchedCapability

HIGH = "HIGH"
_TIGHTNESS = {HumanGateState.NOT_REQUIRED: 0, HumanGateState.GRANTED: 0,
              HumanGateState.PENDING: 1, HumanGateState.DENIED: 2}


class MemoryUnavailable(InputUnavailable):
    """L3 did not answer. Raised into the engine's input sweep ⇒ P7 BLOCK."""


def l3_matches(client: AdmissionInputsClient, intent: Intent) -> list[dict]:
    """HIGH-confidence L3 entries for this intent; raises :class:`MemoryUnavailable`."""
    try:
        mem = (client.provide(intent) or {}).get("incident_memory") or {}
    except InputUnavailable as exc:
        raise MemoryUnavailable(str(exc)) from exc
    if mem.get("status") not in ("OK", "DEGRADED"):
        raise MemoryUnavailable(f"incident memory status {mem.get('status')!r}")
    return [m for m in mem.get("matches") or ()
            if isinstance(m, dict) and (m.get("match") or {}).get("confidence") == HIGH]


def superseded_entries(snapshot: Optional[PolicySnapshotRef]) -> set[str]:
    """L2 standing rules: memory entry ids a snapshot decision record supersedes."""
    if snapshot is None or not snapshot.available:
        return set()
    out: set[str] = set()
    for rev in snapshot.in_scope or snapshot.decisions or ():
        out.update(rev.supersedes or ())
    return out


class MemoryCapabilities:
    """E4 lookup plus L3 known-duplicate reuse targets (data; the engine decides)."""

    def __init__(self, e4, client: AdmissionInputsClient):
        self.e4 = e4
        self.client = client

    def lookup(self, intent: Intent) -> CapabilityLookup:
        base = self.e4.lookup(intent)
        if not base.available:
            return base
        try:
            entries = l3_matches(self.client, intent)
        except MemoryUnavailable:
            return replace(base, available=False)
        have = {m.id for m in tuple(base.matches) + tuple(base.anchor_matches)}
        owners = {m.id: m.owner for m in base.matches}
        extra = []
        for e in entries:
            target = e.get("reuse_target") or _reuse_target(e)
            if e.get("recorded_disposition") != "REUSE" or not target or target in have:
                continue
            owner = owners.get(target) or target.split(".", 1)[0]
            extra.append(MatchedCapability(id=target, owner=owner, repo=owner))
            have.add(target)
        if not extra:
            return base
        # Memory hits go first: a curated known duplicate outranks a lexical match.
        return replace(base, matches=tuple(extra) + tuple(base.matches))


def _reuse_target(entry: dict) -> Optional[str]:
    """``admission_inputs`` passes ``match.matched``; ``reuse_target`` is on the index entry."""
    basis = (entry.get("match") or {}).get("basis") or ()
    if "reuse_target" in basis or "capability_id" in basis:
        matched = (entry.get("match") or {}).get("matched") or ()
        return matched[0] if matched else None
    return None


class MemoryGate:
    """The snapshot gate, tightened by L3 memory; never loosened by it."""

    def __init__(self, inner, client: AdmissionInputsClient):
        self.inner = inner
        self.client = client

    def state(self, intent: Intent, snapshot: PolicySnapshotRef) -> HumanGate:
        gate = self.inner.state(intent, snapshot)
        entries = l3_matches(self.client, intent)          # raises ⇒ engine P7 BLOCK
        lifted = superseded_entries(snapshot)
        strongest = HumanGateState.NOT_REQUIRED
        for e in entries:
            if e.get("id") in lifted:
                continue
            disp = e.get("recorded_disposition")
            want = HumanGateState.DENIED if disp == "BLOCK" else HumanGateState.PENDING
            if _TIGHTNESS[want] > _TIGHTNESS[strongest]:
                strongest = want
        if _TIGHTNESS[strongest] > _TIGHTNESS[gate.state]:
            return HumanGate(strongest)
        return gate


def memory_providers(e4, gate, client: AdmissionInputsClient) -> dict:
    """``AuthorizationEngine(**memory_providers(...), snapshots=..., ...)`` wiring."""
    return {"capabilities": MemoryCapabilities(e4, client), "gates": MemoryGate(gate, client)}


__all__ = ["MemoryCapabilities", "MemoryGate", "MemoryUnavailable", "l3_matches",
           "memory_providers", "superseded_entries"]
