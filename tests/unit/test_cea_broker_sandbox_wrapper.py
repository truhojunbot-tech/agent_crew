"""Quick opt-in wrapper for the isolated launcher/server/decision drill."""
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(os.environ.get("RUN_CEA_BROKER_SANDBOX") != "1",
                    reason="set RUN_CEA_BROKER_SANDBOX=1 for subprocess sandbox")
def test_broker_sandbox():
    script = Path(__file__).resolve().parents[2] / "scripts/cea/broker_sandbox.py"
    subprocess.run([sys.executable, str(script)], check=True, timeout=90)
