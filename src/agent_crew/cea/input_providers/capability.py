"""E4 Capability Ownership Provider client — ``lookup(intent) -> {matches, owner, generation, hash}``.

Adapts the alfred adapter's ``capability`` block (``tools/admission_inputs.py``,
contract of alfred ``ca8b1e8``) onto :class:`~agent_crew.cea.providers.CapabilityLookup`.
E4 is the only matcher (§5.1, §11.3); nothing here matches anything itself.

Fail direction (§5.4, P7, CX-3 / CXC-2): provider did not answer or registry
``UNAVAILABLE`` ⇒ ``available=False``; registry ``DEGRADED`` (e.g. past
``max_age_days``) or any matched record flagged stale ⇒ ``stale=True``. The
engine BLOCKs both. The registry is unkeyed today, so ``signature`` is
``UNKEYED`` — recorded, not trusted.
"""
from __future__ import annotations

from typing import Optional

from agent_crew.cea.input_providers.admission_inputs import AdmissionInputsClient, InputUnavailable
from agent_crew.cea.intent import Intent
from agent_crew.cea.providers import CapabilityLookup, SignatureStatus
from agent_crew.cea.receipt import MatchedCapability, RegistryRef


class E4CapabilityProvider:
    def __init__(self, client: Optional[AdmissionInputsClient] = None):
        self.client = client or AdmissionInputsClient()

    def lookup(self, intent: Intent) -> CapabilityLookup:
        try:
            cap = (self.client.provide(intent) or {}).get("capability") or {}
        except InputUnavailable:
            return CapabilityLookup(registry=None, available=False)
        reg = cap.get("registry") or {}
        status = cap.get("status") or reg.get("status")
        if status == "UNAVAILABLE" or not reg.get("hash") or reg.get("generation") is None:
            return CapabilityLookup(registry=None, available=False)
        ref = RegistryRef(generation=str(reg["generation"]), hash=str(reg["hash"]))
        matches, stale = [], status != "OK"
        for m in cap.get("matches") or ():
            if not isinstance(m, dict) or not m.get("capability_id"):
                continue
            stale = stale or bool(m.get("stale_record"))
            owner = str(m.get("owning_project") or "UNKNOWN")
            matches.append(MatchedCapability(id=str(m["capability_id"]), owner=owner, repo=owner))
        declared = cap.get("declared_capability") or {}
        if declared.get("capability_id") and declared.get("found") is False:
            # The intent names a capability the registry does not know: that is
            # an absence E4 cannot vouch for, so it is not "no match" (§5.4).
            stale = True
        return CapabilityLookup(registry=ref, matches=tuple(matches), available=True, stale=stale,
                                signature=SignatureStatus.UNKEYED)
