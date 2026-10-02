#!/usr/bin/env python3
"""Isolated broker/server smoke plus the three broker decision assertions.

All writable state and listeners live under a temporary HOME. No live port or
~/.agent_crew path is read or changed. Evidence names exactly what was tested.
"""
from __future__ import annotations

import json
import hashlib
import hmac
import os
import site
import sqlite3
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
LAUNCHER = ROOT / "scripts/cea/broker-launch.sh"
TEST = ROOT / "tests/unit/test_cea_broker_oop.py"


def evidence_path(name: str) -> Path:
    """Keep operator evidence in the repo unless a caller selects another dir."""
    directory = os.environ.get("CEA_BROKER_SANDBOX_EVIDENCE_DIR")
    return (Path(directory) if directory else ROOT / "evidence") / name


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_e2e():
    """Exercise HTTP admission through the degraded broker in disposable state."""
    build = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    out = evidence_path(f"cea_broker_e2e_{build[:7]}.json")
    with tempfile.TemporaryDirectory(prefix="cea-broker-e2e-") as tmp:
        home = Path(tmp)
        project_dir = home / "alfred"
        project_dir.mkdir()
        db = project_dir / "tasks.db"
        port = free_port()
        # The only inherited variables are ordinary process/runtime settings. No
        # caller's CEA or Agent Crew paths can escape this disposable HOME.
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_CREW_")}
        env.update(HOME=tmp, PYTHONUSERBASE=site.getuserbase(),
                   PYTHONPATH=os.pathsep.join((tmp, str(SRC), site.getusersitepackages())),
                   AGENT_CREW_DB=str(db), AGENT_CREW_STATE=str(project_dir / "state.json"),
                   AGENT_CREW_PORT=str(port), AGENT_CREW_DELIVERY="mcp",
                   AGENT_CREW_WATCHDOG_DISABLED="1", AGENT_CREW_ANOMALY_DISABLED="1",
                   AGENT_CREW_AUTHZ_CONFIG=str(home / "absent-broker.env"),
                   AGENT_CREW_AUTHZ_PYTHON=sys.executable,
                   AGENT_CREW_AUTHZ_PYTHONPATH=os.pathsep.join((str(SRC), site.getusersitepackages())),
                   AGENT_CREW_AUTHZ_SOCK_DIR=str(home / "sock"),
                   AGENT_CREW_AUTHZ_CLIENT_GROUP=subprocess.check_output(["id", "-gn"], text=True).strip(),
                   AGENT_CREW_CEA_MODE="enforce", AGENT_CREW_CEA_MODE__ALFRED="enforce",
                   AGENT_CREW_CEA_ENFORCE_CODES="RUNTIME_STATE_FORBIDS",
                   AGENT_CREW_CEA_ENFORCE_CODES_ALFRED="RUNTIME_STATE_FORBIDS")
        key = b"sandbox-only-cea-broker-e2e-key"
        key_path = home / "snapshot.key"
        key_path.write_bytes(key)
        body = {"generation": 12, "produced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "tier": "T0", "decisions": [{"decision_id": "T0-SANDBOX-RESUME",
                "body_hash": "sha256:" + "a" * 32, "supersedes": [],
                "principals": ["owner:sandbox"], "build_commits": [build],
                "runtimes": ["alfred"], "scope": {"project": "alfred"}}],
                "review_test_matrix": {}, "human_gate_predicates": []}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        snapshot = dict(body, signature={"alg": "hmac-sha256",
                                   "key_id": hashlib.sha256(key).hexdigest()[:16],
                                   "value": hmac.new(key, canonical, hashlib.sha256).hexdigest()})
        snapshot_path = home / "snapshot.json"
        snapshot_path.write_text(json.dumps(snapshot))
        hwm_dir = home / "state"
        hwm_dir.mkdir(mode=0o700)
        hwm_path = hwm_dir / "snapshot-hwm.json"
        hwm_path.write_text(json.dumps({
            "generation": body["generation"],
            "content_hash": "sha256:" + hashlib.sha256(canonical).hexdigest(),
        }))
        hwm_path.chmod(0o600)
        env.update(AGENT_CREW_CEA_SNAPSHOT_PATH=str(snapshot_path),
                   AGENT_CREW_CEA_SNAPSHOT_KEY_FILE=str(key_path),
                   AGENT_CREW_CEA_SNAPSHOT_HWM_FILE=str(hwm_path))
        # Mirror the installed broker boundary with a disposable signing key:
        # only the broker gets the private half; admission verifies the public half.
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        receipt_private = Ed25519PrivateKey.generate()
        receipt_private_path = home / "receipt-signing.key"
        receipt_public_path = home / "receipt-signing.pub"
        receipt_private_path.write_bytes(receipt_private.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
            serialization.NoEncryption()))
        receipt_private_path.chmod(0o400)
        receipt_public_path.write_bytes(receipt_private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw))
        receipt_public_path.chmod(0o644)
        src_commit_path = home / "SRC_COMMIT"
        src_commit_path.write_text(build + "\n")
        quota_dir = home / "quota"
        quota_dir.mkdir()
        auth_path = home / "codex-auth.json"
        auth_path.write_text(json.dumps({"tokens": {"account_id": "sandbox-account"}}))
        codex_cache = quota_dir / "codex_monitor" / "quota_cache.json"
        codex_cache.parent.mkdir()
        codex_cache.write_text(json.dumps({"fetched_at": time.time(),
            "account_fingerprint": "sha256:" + hashlib.sha256(b"sandbox-account").hexdigest()[:16],
            "five_hour": {"utilization": 0.1}}))
        env.update(AGENT_CREW_CEA_QUOTA_CACHE_DIR=str(quota_dir),
                   AGENT_CREW_CEA_CODEX_AUTH_PATH=str(auth_path))
        token_path = home / "adapter.token"
        token_path.write_text("sandbox-token")
        token_path.chmod(0o600)
        tokens_path = home / "tokens.json"
        tokens_path.write_text(json.dumps({"adapters": {"sandbox-token": {
            "principal": "cron:sandbox", "provenance": "cron"}}}))
        tokens_path.chmod(0o600)
        env["AGENT_CREW_CEA_ADAPTER_TOKEN_FILE"] = str(token_path)
        (home / "sandbox_app.py").write_text(
            "import os\nfrom agent_crew.server import create_app\n"
            "app = create_app(os.environ['AGENT_CREW_DB'], project='alfred', "
            "state_path=os.environ['AGENT_CREW_STATE'], watchdog_disabled=True, "
            "anomaly_disabled=True)\n")
        init = subprocess.run([sys.executable, "-c", "from agent_crew.queue import TaskQueue; "
                               "import os; TaskQueue(os.environ['AGENT_CREW_DB'])"],
                              env=env, capture_output=True, text=True, timeout=20)
        if init.returncode:
            raise RuntimeError("sandbox DB init failed: " + init.stderr)
        broker_env = dict(env, AGENT_CREW_CEA_BROKER_DB=str(db),
                          AGENT_CREW_CEA_CALLER_TOKENS=str(tokens_path),
                          AGENT_CREW_CEA_ISSUER="agent_crew.cea.broker.sandbox",
                          AGENT_CREW_CEA_RECEIPT_SIGNING_KEY_FILE=str(receipt_private_path),
                          AGENT_CREW_AUTHZ_SRC_COMMIT_PATH=str(src_commit_path))
        server_env = dict(env, AGENT_CREW_CEA_BROKER_SOCKET=str(home / "sock" / "broker.sock"),
                          AGENT_CREW_CEA_BROKER_DEGRADED="1",
                          AGENT_CREW_CEA_RECEIPT_PUBKEY_FILE=str(receipt_public_path))
        evidence = {"build_sha": build, "mode": "sandbox_http_e2e", "project": "alfred",
                    "broker_socket": str(home / "sock" / "broker.sock"),
                    "enforced_codes": ["RUNTIME_STATE_FORBIDS"], "pass": False}
        broker = server = None
        try:
            broker = subprocess.Popen([str(LAUNCHER), "--degraded"], env=broker_env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            for _ in range(100):
                if (home / "sock" / "broker.sock").exists():
                    break
                if broker.poll() is not None:
                    raise RuntimeError("broker exited: " + broker.stderr.read())
                time.sleep(.1)
            else:
                raise RuntimeError("broker socket not bound")
            server = subprocess.Popen([sys.executable, "-m", "uvicorn", "sandbox_app:app",
                                       "--host", "127.0.0.1", "--port", str(port)],
                                      env=server_env, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE, text=True)
            for _ in range(100):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                        if response.status == 200:
                            break
                except Exception:
                    if server.poll() is not None:
                        raise RuntimeError("server exited: " + server.stderr.read())
                    time.sleep(.1)
            else:
                raise RuntimeError("server health timed out")

            def cli(*args):
                cmd = [sys.executable, "-c", "from agent_crew.cli import crew; crew()", *args]
                result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=20)
                if result.returncode:
                    raise RuntimeError(f"CLI {args[0]} failed: {result.stdout} {result.stderr}")
                return json.loads(result.stdout)

            def post(task_id, task_type):
                payload = {"task_id": task_id, "task_type": task_type,
                           "description": "sandbox CEA broker HTTP admission check",
                           "project": "alfred"}
                req = urllib.request.Request(f"http://127.0.0.1:{port}/tasks",
                    data=json.dumps(payload).encode(), method="POST",
                    headers={"Content-Type": "application/json", "X-Crew-Project": "alfred"})
                try:
                    with urllib.request.urlopen(req, timeout=10) as response:
                        status, raw = response.status, response.read()
                except urllib.error.HTTPError as exc:
                    status, raw = exc.code, exc.read()
                with sqlite3.connect(db) as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute("SELECT receipt_id, issuer, decision, reason, provider_budget, downgrade_reason, "
                                       "provenance FROM authorization_receipts WHERE task_id = ? "
                                       "ORDER BY row_id DESC LIMIT 1", (task_id,)).fetchone()
                    task_row = conn.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
                receipt = dict(row) if row else None
                if receipt:
                    receipt["reason"] = json.loads(receipt["reason"])
                    receipt["provider_budget"] = json.loads(receipt["provider_budget"])
                    receipt["provenance"] = json.loads(receipt["provenance"]) if receipt["provenance"] else None
                return {"http_status": status, "http_body": json.loads(raw) if raw[:1] in (b"{", b"[") else raw.decode(errors="replace"),
                        "receipt": receipt, "task_status": task_row["status"] if task_row else None}

            paused = cli("pause", "alfred", "--base", tmp, "--source", "sandbox",
                         "--reason", "broker e2e gate", "--incident", "alfred#51")
            evidence["pause"] = paused
            evidence["block"] = post("broker-e2e-block", "implement")
            resumed = cli("resume", "alfred", "--base", tmp,
                          "--generation", str(paused["generation"] + 1),
                          "--source", "sandbox", "--decision-id", "T0-SANDBOX-RESUME")
            evidence["resume"] = resumed
            evidence["allow"] = post("broker-e2e-allow", "review")
            for case in ("block", "allow"):
                receipt = evidence[case]["receipt"]
                if not receipt or receipt["issuer"] != "agent_crew.cea.broker.sandbox":
                    raise RuntimeError(f"{case}: broker receipt provenance missing")
                if receipt["downgrade_reason"] != "BROKER_TREE_USER_WRITABLE":
                    raise RuntimeError(f"{case}: unexpected downgrade/fallback reason: "
                                       f"{receipt['downgrade_reason']}")
            block_response = evidence["block"]
            pause_suppressed = (block_response["http_status"] == 200
                and isinstance(block_response["http_body"], dict)
                and block_response["http_body"].get("suppressed_by_pause") is True)
            if (block_response["receipt"]["reason"]["code"] != "RUNTIME_STATE_FORBIDS"
                    or not (block_response["http_status"] >= 400 or pause_suppressed)
                    or block_response["task_status"] in {"pending", "running"}):
                raise RuntimeError("paused implement was not blocked at HTTP admission")
            if evidence["allow"]["http_status"] != 201 or evidence["allow"]["task_status"] != "pending":
                raise RuntimeError("resumed review was not admitted")
            if evidence["allow"]["receipt"]["decision"] != "ALLOW":
                raise RuntimeError("resumed review did not receive an ALLOW receipt")
            evidence["pass"] = True
        except Exception as exc:
            evidence["error"] = str(exc)
            evidence["traceback"] = traceback.format_exc()
        finally:
            for proc in (server, broker):
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
            if not evidence["pass"] and server is not None and server.stderr is not None:
                evidence["server_stderr"] = server.stderr.read()[-6000:]
            if not evidence["pass"] and broker is not None and broker.stderr is not None:
                evidence["broker_stderr"] = broker.stderr.read()[-3000:]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(evidence, indent=2) + "\n")
            print(out)
        return 0 if evidence["pass"] else 1


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
            if (home / "sock" / "broker.sock").lstat().st_gid != (home / "sock").lstat().st_gid:
                raise RuntimeError("broker socket gid differs from validated client directory")
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
            out = evidence_path(f"cea_broker_sandbox_{build[:7]}.json")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(evidence, indent=2) + "\n")
            print(out)
            # --check validates the installed pinned runtime, which this
            # disposable degraded sandbox intentionally does not install.
            # Keep its result in evidence; the smoke verdict comes from the
            # bound socket, server health, and isolated decision assertion.
            return 0 if test.returncode == 0 else 1
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
    raise SystemExit(run_e2e() if "--e2e" in sys.argv[1:] else run())
