"""Best-effort ADR-004 shadow observation at the task ingress boundary.

The matcher and JSONL writer belong to Alfred's existing contract_registry.py.
Keep the work text off argv and let that writer store only its SHA-256 and length.
This observation never changes a CEA admission decision.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import sys

from agent_crew.cea.input_providers.admission_inputs import AdmissionInputsClient

_WORKER = """
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location('adr004_contract_registry', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
request = json.load(sys.stdin)
module.admission_decision(request['text'], request['project'], mode='shadow')
"""


def record_ingress_decision(description: str, project: str, *,
                            provider_path: str | None = None) -> bool:
    """Ask the existing logger for one redacted shadow decision; fail open."""
    # A local test runner must not add fixture admissions to the real
    # promotion log when Alfred happens to be installed on the same host.
    if provider_path is None and os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    provider = Path(provider_path or AdmissionInputsClient().path)
    logger_path = provider.with_name("contract_registry.py")
    if not logger_path.is_file():
        logging.getLogger(__name__).warning("ADR-004 shadow logger unavailable")
        return False
    try:
        result = subprocess.run(
            [sys.executable, "-c", _WORKER, str(logger_path)],
            input=json.dumps({"text": description or "", "project": project}),
            text=True, capture_output=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logging.getLogger(__name__).warning("ADR-004 shadow logger failed")
        return False
    if result.returncode:
        logging.getLogger(__name__).warning("ADR-004 shadow logger exited %d", result.returncode)
        return False
    return True
