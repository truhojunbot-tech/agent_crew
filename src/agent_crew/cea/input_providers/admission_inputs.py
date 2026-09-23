"""Client for alfred ``tools/admission_inputs.py`` — the L2 (E4) + L3 provider path.

Contract (alfred ``sev0/cea-alfred-lineage`` @ ``ca8b1e8``; ``tools/admission_inputs.py``
docstring, schema ``admission-inputs/v1``)::

    python3 <path> < {"intent": {<P4 intent>}, "text": "<description>"} > inputs.json

Exit 0 whenever a JSON answer was written (even one that says UNAVAILABLE), 2 on
a malformed request. The answer is data only: ``capability.matches`` (E4, the
only matcher), ``capability.registry.{status, generation, hash}``, and
``incident_memory.matches[].recorded_disposition``.

⛔Anything short of a parseable ``admission-inputs/v1`` answer is an
  :class:`InputUnavailable`, never "no matches": a missing script, a timeout, a
  non-zero exit, bad JSON and an ``error`` body all mean the input did not
  answer (P7).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Callable, Optional

from agent_crew.cea.intent import Intent

DEFAULT_ADMISSION_INPUTS = "/home/truhojun/alfred/tools/admission_inputs.py"
SCHEMA = "admission-inputs/v1"


class InputUnavailable(Exception):
    """The input provider did not answer. The engine records it and BLOCKs (P7)."""


def intent_payload(intent: Intent) -> dict:
    """The P4 intent as ``admission_inputs.py`` reads it (project, capability_id, target)."""
    ident = intent.identity
    wc = getattr(ident.work_class, "value", ident.work_class)
    return {"project": ident.project, "work_class": wc, "capability_id": ident.capability_id,
            "authority_decision_ids": list(ident.authority_decision_ids),
            "target": {"repo": ident.target.repo, "base_ref": ident.target.base_ref,
                       "scope_anchors": list(ident.target.scope_anchors)}}


class AdmissionInputsClient:
    """stdin/stdout client. ``runner`` is injectable so tests never spawn alfred."""

    def __init__(self, path: Optional[str] = None, *, python: Optional[str] = None,
                 timeout: float = 10.0, runner: Optional[Callable[[dict], dict]] = None,
                 env: Optional[dict] = None, child_env: Optional[dict] = None):
        e = os.environ if env is None else env
        self.path = path or (e.get("AGENT_CREW_CEA_ADMISSION_INPUTS") or "").strip() \
            or DEFAULT_ADMISSION_INPUTS
        self.python = python or sys.executable or "python3"
        self.timeout = timeout
        self._runner = runner
        self._cache: dict[str, dict] = {}
        self._child_env = dict(child_env or {})
        """Extra environment for the alfred script — how the wiring tells it which
        registry / incident-memory files to read. Merged over ``os.environ`` at
        spawn time, never consulted for our own decisions."""

    def provide(self, intent: Intent) -> dict:
        """The raw ``admission-inputs/v1`` answer; raises :class:`InputUnavailable`."""
        req = {"intent": intent_payload(intent), "text": intent.description or ""}
        key = json.dumps(req, sort_keys=True)
        if key in self._cache:
            return self._cache[key]
        out = self._runner(req) if self._runner is not None else self._run(req)
        if not isinstance(out, dict) or out.get("schema") != SCHEMA or "error" in out:
            raise InputUnavailable(f"admission_inputs answered {str(out)[:200]!r}")
        # One authorize() reads E4 and L3 from the same answer; a new engine
        # call re-reads (the cache is per client, and the engine builds B once).
        self._cache = {key: out}
        return out

    def _run(self, req: dict) -> dict:
        if not os.path.isfile(self.path):
            raise InputUnavailable(f"{self.path} does not exist (L2/L3 provider path unset?)")
        try:
            child = None if not self._child_env else {**os.environ, **self._child_env}
            proc = subprocess.run([self.python, self.path], input=json.dumps(req),
                                  capture_output=True, text=True, timeout=self.timeout,
                                  check=False, env=child)
        except (OSError, subprocess.SubprocessError) as exc:
            raise InputUnavailable(f"admission_inputs did not run: {exc}") from exc
        if proc.returncode != 0:
            raise InputUnavailable(f"admission_inputs exit {proc.returncode}: {proc.stdout[:200]}")
        try:
            return json.loads(proc.stdout)
        except ValueError as exc:
            raise InputUnavailable(f"admission_inputs wrote non-JSON: {exc}") from exc
