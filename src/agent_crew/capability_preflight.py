"""Reuse-first admission adapter for the fleet capability registry (alfred#51 E4). STUB.

Agent Crew does not own, copy or re-implement the capability registry or its
match rule. There is exactly one implementation: the alfred CLI
``tools/contract_registry.py capability-lookup`` over
``governance/capability_registry.json`` (+ alfred#4 contracts). This module is
a thin, standalone (agent_crew#310) client of that CLI:

* configured by ``AGENT_CREW_CAPABILITY_LOOKUP`` (the command prefix, e.g.
  ``python3 /path/to/alfred/tools/contract_registry.py``); no import of alfred code;
* the output contract is ``schema: capability-lookup/v1``; exit 0 = ALLOW,
  exit 10 = STOP_AND_REVIEW;
* anything else — not configured, timeout, non-zero rc, bad JSON, unknown
  schema, rc/decision disagreement — is **fail-closed**: STOP_AND_REVIEW.

NOT WIRED: nothing in server.py / queue.py calls this yet. The integration
point (direct ``POST /tasks`` for ``task_type=implement``, directive §9.3,
gap map E5(a)) and its recorded override are a separate, human-gated lane.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shlex
import subprocess
from typing import Callable, Mapping, Optional

ENV_COMMAND = "AGENT_CREW_CAPABILITY_LOOKUP"
SCHEMA = "capability-lookup/v1"
EXIT_ALLOW, EXIT_STOP = 0, 10


@dataclasses.dataclass(frozen=True)
class AdmissionDecision:
    allow: bool
    decision: str            # ALLOW | STOP_AND_REVIEW
    reason: str
    matches: tuple = ()
    registry_status: Optional[str] = None
    registry_source: Optional[str] = None


def _closed(reason: str, **kw) -> AdmissionDecision:
    return AdmissionDecision(allow=False, decision="STOP_AND_REVIEW", reason=reason, **kw)


def check_admission(description: str, project: str, *, command: Optional[str] = None,
                    env: Mapping[str, str] = os.environ,
                    runner: Callable = subprocess.run, timeout: float = 15) -> AdmissionDecision:
    """Ask the fleet registry whether new work may be admitted. Never raises."""
    cmd = command or env.get(ENV_COMMAND)
    if not cmd:
        return _closed("LOOKUP_NOT_CONFIGURED")
    argv = shlex.split(cmd) + ["capability-lookup", "--text", description or "", "--project", project or ""]
    try:
        p = runner(argv, capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # timeout, missing binary, ...
        return _closed(f"LOOKUP_FAILED: {type(e).__name__}")
    try:
        out = json.loads(p.stdout)
    except Exception:
        return _closed(f"LOOKUP_FAILED: unparsable output (rc={p.returncode})")
    if not isinstance(out, dict) or out.get("schema") != SCHEMA:
        return _closed(f"LOOKUP_FAILED: unexpected schema {out.get('schema') if isinstance(out, dict) else None!r}")
    decision = out.get("decision")
    common = dict(matches=tuple(m.get("capability_id") for m in out.get("matches") or []),
                  registry_status=out.get("registry_status"), registry_source=out.get("registry_source"))
    if p.returncode == EXIT_ALLOW and decision == "ALLOW":
        return AdmissionDecision(allow=True, decision="ALLOW", reason=out.get("reason", "NO_MATCH"), **common)
    if p.returncode == EXIT_STOP and decision == "STOP_AND_REVIEW":
        return _closed(out.get("reason") or "STOP_AND_REVIEW", **common)
    return _closed(f"LOOKUP_FAILED: rc={p.returncode} disagrees with decision={decision!r}", **common)
