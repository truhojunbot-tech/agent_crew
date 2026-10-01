"""Concrete P1 input providers (SEV-0 CEA lineage step 3; ADR Π P1, P6, P7, §5, O9).

``agent_crew.cea.providers`` holds the *protocols*; this package holds the first
real implementations. It is a package of its own (not ``cea/providers/``)
because ``cea/providers.py`` already exists and a same-named package would
shadow it.

Every class here returns data and never a verdict. Unavailable, stale or
unverifiable inputs are *reported* (``available=False`` / ``stale=True`` /
``read_failed=True`` / a non-VALID signature / a raised exception) and the
engine turns that into BLOCK under P7. Nothing here decides.

Path configuration (all read-only; nothing here writes):

=====================================  ====================================================
env                                    meaning
=====================================  ====================================================
``AGENT_CREW_CEA_ADMISSION_INPUTS``    alfred ``tools/admission_inputs.py`` (L2 E4 + L3)
``AGENT_CREW_CEA_CAPABILITY_REGISTRY`` alfred ``governance/capability_registry.json``
``AGENT_CREW_CEA_INCIDENT_MEMORY``     alfred ``governance/incident_memory.json``
``AGENT_CREW_CEA_POLICY_SNAPSHOT``     alfred ``governance/control_policy_snapshot.json``
``AGENT_CREW_CEA_QUOTA_CACHE_DIR``     Qouta ``<dir>/<provider>_monitor/quota_cache.json``
``AGENT_CREW_CEA_COOLDOWN_FILE``       #308 cooldown JSON ``{provider: until_epoch}``
=====================================  ====================================================
"""
from agent_crew.cea.input_providers.admission_inputs import (
    AdmissionInputsClient, DEFAULT_ADMISSION_INPUTS, InputUnavailable)
from agent_crew.cea.input_providers.budget import QuotaBudgetProvider
from agent_crew.cea.input_providers.capability import E4CapabilityProvider
from agent_crew.cea.input_providers.gate import SnapshotRecordGate
from agent_crew.cea.input_providers.runtime import QueueRuntimeStateProvider
from agent_crew.cea.input_providers.snapshot import CanonicalPolicySnapshotReader

__all__ = ["AdmissionInputsClient", "CanonicalPolicySnapshotReader", "DEFAULT_ADMISSION_INPUTS",
           "E4CapabilityProvider", "InputUnavailable", "QueueRuntimeStateProvider",
           "QuotaBudgetProvider", "SnapshotRecordGate"]
