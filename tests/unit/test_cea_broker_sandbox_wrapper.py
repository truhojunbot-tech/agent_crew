"""Quick opt-in wrapper for the isolated launcher/server/decision drill."""
import os
import site
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(os.environ.get("RUN_CEA_BROKER_SANDBOX") != "1",
                    reason="set RUN_CEA_BROKER_SANDBOX=1 for subprocess sandbox")
def test_broker_sandbox():
    script = Path(__file__).resolve().parents[2] / "scripts/cea/broker_sandbox.py"
    subprocess.run([sys.executable, str(script)], check=True, timeout=90,
                   env={**os.environ, "PYTHONUSERBASE": site.getuserbase()})


@pytest.mark.skipif(importlib.util.find_spec("uvicorn") is None,
                    reason="uvicorn unavailable")
def test_broker_sandbox_e2e():
    script = Path(__file__).resolve().parents[2] / "scripts/cea/broker_sandbox.py"
    result = subprocess.run([sys.executable, str(script), "--e2e"],
                            capture_output=True, text=True, timeout=120,
                            env={**os.environ, "PYTHONUSERBASE": site.getuserbase()})
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(Path(result.stdout.strip().splitlines()[-1]).read_text())
    assert evidence["pass"] is True
    assert evidence["block"]["receipt"]["reason"]["code"] == "RUNTIME_STATE_FORBIDS"
    block = evidence["block"]
    assert (block["http_status"] >= 400 or
            (block["http_status"] == 200 and block["http_body"].get("suppressed_by_pause") is True))
    assert evidence["block"]["task_status"] is None
    assert evidence["allow"]["http_status"] == 201
    assert evidence["allow"]["receipt"]["decision"] == "ALLOW"
    assert evidence["allow"]["task_status"] == "pending"
    for case in ("block", "allow"):
        assert evidence[case]["receipt"]["issuer"] == "agent_crew.cea.broker.sandbox"
        assert evidence[case]["receipt"]["downgrade_reason"] == "BROKER_TREE_USER_WRITABLE"
