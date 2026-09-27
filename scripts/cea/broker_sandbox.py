#!/usr/bin/env python3
"""Isolated broker/server smoke plus the three broker decision assertions.

All writable state and listeners live under a temporary HOME. No live port or
~/.agent_crew path is read or changed. Evidence names exactly what was tested.
"""
from __future__ import annotations

import json
import os
import site
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
LAUNCHER = ROOT / "scripts/cea/broker-launch.sh"
TEST = ROOT / "tests/unit/test_cea_broker_oop.py"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run():
    build = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    with tempfile.TemporaryDirectory(prefix="cea-broker-sandbox-") as tmp:
        home = Path(tmp)
        port = free_port()
        env = dict(os.environ, HOME=tmp, PYTHONPATH=os.pathsep.join((str(SRC), site.getusersitepackages())),
                   AGENT_CREW_DB=str(home / "tasks.db"),
                   AGENT_CREW_STATE=str(home / "state.json"),
                   AGENT_CREW_PORT=str(port),
                   AGENT_CREW_AUTHZ_CONFIG=str(home / "absent-broker.env"),
                   AGENT_CREW_AUTHZ_PYTHON=sys.executable,
                   AGENT_CREW_AUTHZ_PYTHONPATH=str(SRC),
                   AGENT_CREW_AUTHZ_SOCK_DIR=str(home / "sock"),
                   AGENT_CREW_CEA_MODE__ALFRED="enforce",
                   AGENT_CREW_CEA_ENFORCE_CODES_ALFRED="RUNTIME_STATE_FORBIDS")
        env.pop("AGENT_CREW_CEA_BROKER_SOCKET", None)
        env.pop("AGENT_CREW_CEA_BROKER_DB", None)
        env["AGENT_CREW_AUTHZ_CLIENT_GROUP"] = subprocess.check_output(
            ["id", "-gn"], text=True).strip()
        check = subprocess.run([str(LAUNCHER), "--check"], env=env,
                               capture_output=True, text=True, timeout=15)
        broker = None
        server = None
        try:
            broker = subprocess.Popen([str(LAUNCHER), "--degraded"], env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True)
            for _ in range(50):
                if (home / "sock" / "broker.sock").exists():
                    break
                if broker.poll() is not None:
                    raise RuntimeError("degraded launcher exited before socket bind")
                time.sleep(.1)
            else:
                raise RuntimeError("degraded broker socket did not appear")
            server = subprocess.Popen([sys.executable, "-m", "uvicorn", "agent_crew.server:app",
                                       "--host", "127.0.0.1", "--port", str(port)], env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                      text=True)
            health = None
            for _ in range(80):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                        health = response.status
                    break
                except Exception:
                    if server.poll() is not None:
                        raise RuntimeError("sandbox server exited before health check: " +
                                           (server.stderr.read() if server.stderr else ""))
                    time.sleep(.1)
            if health != 200:
                raise RuntimeError("sandbox server health check failed")
            test = subprocess.run([sys.executable, "-m", "pytest", "-q",
                                   f"{TEST}::test_broker_remote_decisions_enforce_only_runtime_code"],
                                  cwd=ROOT, env=env, capture_output=True, text=True, timeout=45)
            evidence = {"build_sha": build, "temp_home": tmp, "server_port": port,
                        "server_health_status": health,
                        "launcher_check": {"exit_code": check.returncode, "stdout": check.stdout},
                        "degraded_broker_started": True,
                        "decision_test": {"exit_code": test.returncode,
                                          "output": test.stdout[-2000:] + test.stderr[-1000:]},
                        "cases": {"RUNTIME_STATE_FORBIDS": "BLOCK",
                                  "review_allowed": "ALLOW",
                                  "BUDGET_EXHAUSTED": "advisory"} if test.returncode == 0 else {},
                        "scope": "decision cases use a separate isolated broker with injected test providers; server health and launcher are smoke checks"}
            out = ROOT / "evidence" / f"cea_broker_sandbox_{build[:7]}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(evidence, indent=2) + "\n")
            print(out)
            return 0 if check.returncode == 0 and test.returncode == 0 else 1
        finally:
            for proc in (server, broker):
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()


if __name__ == "__main__":
    raise SystemExit(run())
