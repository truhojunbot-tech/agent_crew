"""Sandbox #428: Qouta failure must not let duplicate capability work dispatch.

Run: PYTHONPATH=src python scripts/chaos/qouta_failure_containment.py [--output PATH]
All queue, HOME, quota and registry files live under one TemporaryDirectory.
The real E4 adapter consumes a deterministic sandbox registry response; no
production governance, Qouta, tmux pane or port is read or changed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import time
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_crew.cea.engine import EngineConfig
from agent_crew.cea.input_providers.budget import QuotaBudgetProvider
from agent_crew.cea.input_providers.capability import E4CapabilityProvider
from agent_crew.cea.providers import PolicySnapshotRef, SignatureStatus
from agent_crew.cea.receipt import DecisionRev, HumanGate, HumanGateState
from agent_crew.cea.runtime_state import RuntimeState, RuntimeStateSnapshot
from agent_crew.cea import store as receipt_store
from agent_crew.cea import wiring as cea_wiring
from agent_crew.queue import TaskQueue


class Snapshot:
    def current(self, intent=None):
        decision = DecisionRev(decision_id="SANDBOX-428", body_hash="b" * 32)
        return PolicySnapshotRef(generation=1, hash="s" * 16, produced_at=None,
                                 decisions=(decision,), in_scope=(decision,),
                                 signature=SignatureStatus.VALID, tier="T0")


class RegistryClient:
    """Sandbox E4 response with one existing capability owned by another project."""
    def __init__(self, path: Path):
        self.path = path

    def provide(self, intent):
        registry = json.loads(self.path.read_text())
        matched = intent.identity.capability_id == registry["capability_id"]
        return {"capability": {
            "status": "OK", "registry": {"generation": 1, "hash": "r" * 16},
            "matches": ([{"capability_id": registry["capability_id"],
                         "owning_project": registry["owning_project"]}] if matched else []),
        }}


class Runtime:
    def current(self):
        return RuntimeStateSnapshot(state=RuntimeState.ACTIVE, epoch=1)


class Gate:
    def state(self, intent, snapshot):
        return HumanGate(HumanGateState.NOT_REQUIRED)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def current_receipt(db: Path):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT receipt_id FROM authorization_receipts ORDER BY row_id DESC LIMIT 1").fetchone()
        return receipt_store.current_receipt(conn, row["receipt_id"]) if row else None


def run_case(root: Path, failure: str, task_kind: str):
    case = root / f"{failure}-{task_kind}"
    case.mkdir()
    home = case / "home"
    home.mkdir()
    quota = case / "qouta"
    quota.mkdir()
    cache = quota / "claude_monitor" / "quota_cache.json"
    if failure != "missing":
        cache.parent.mkdir()
        if failure == "empty":
            cache.write_text("")
        else:
            cache.write_text(json.dumps({"fetched_at": time.time() - 3600,
                                         "five_hour": {"utilization": 0.1}}))
    registry = case / "capability_registry.json"
    registry.write_text(json.dumps({"capability_id": "shared-blackboard-poster",
                                    "owning_project": "existing-owner"}))
    shim = case / "tmux"
    shim.write_text("#!/bin/sh\nexit 99\n")
    shim.chmod(0o755)
    db = case / "tasks.db"
    providers = {"snapshots": Snapshot(), "capabilities": E4CapabilityProvider(RegistryClient(registry)),
                 "runtime": Runtime(), "budgets": QuotaBudgetProvider(str(quota),
                     credit_class={"claude": "plan"}, env={"AGENT_CREW_CEA_CODEX_AUTH_PATH": str(home / "no-auth")}),
                 "gates": Gate()}
    original_init = TaskQueue.__init__

    def sandbox_init(self, db_path, *args, **kwargs):
        kwargs["cea_config"] = EngineConfig(mode="test")
        kwargs["cea_providers"] = providers
        original_init(self, db_path, *args, **kwargs)

    # TestClient drives the real HTTP routes in-process. A free loopback port is
    # reserved for create_app's URL formatting; no listener is started.
    port = free_port()
    env = {"HOME": str(home), "AGENT_CREW_DELIVERY": "both",
           "AGENT_CREW_CEA_MODE": "test", "AGENT_CREW_CEA_QUOTA_CACHE_DIR": str(quota),
           "AGENT_CREW_CEA_REGISTRY_PATH": str(registry),
           "AGENT_CREW_WATCHDOG_DISABLED": "1", "AGENT_CREW_ANOMALY_DISABLED": "1",
           "PATH": str(case) + os.pathsep + os.environ.get("PATH", "")}
    task_id = f"chaos-428-{failure}-{task_kind}"
    capability = "shared-blackboard-poster" if task_kind == "duplicate" else "novel-sandbox-capability"
    body = {"task_id": task_id, "task_type": "implement", "project": "chaos-sandbox",
            "description": "Implement " + capability, "branch": "sandbox-only", "priority": 3,
            "context": {"capability_id": capability, "scope_anchors": ["sandbox/" + capability],
                        "authority_decision_ids": ["SANDBOX-428"]}}
    wired = cea_wiring.Wiring(providers=providers, mode="test")
    with patch.dict(os.environ, env, clear=True), patch.object(TaskQueue, "__init__", sandbox_init), \
         patch.object(cea_wiring, "install_from_env", return_value=wired):
        from agent_crew.server import create_app
        app = create_app(str(db), pane_map={}, port=port, project="chaos-sandbox",
                         state_path=str(case / "state.json"), worktree_map={},
                         watchdog_disabled=True, anomaly_disabled=True, fallback_disabled=True)
        with TestClient(app) as client:
            response = client.post("/tasks", json=body)
            # This route would deliver a runnable implementation if admission failed open.
            dispatched = None
            if task_kind == "duplicate":
                poll = client.get("/tasks/next", params={"role": "implementer", "agent": "codex"})
                if poll.status_code == 200:
                    dispatched = poll.json()
    receipt = current_receipt(db)
    if receipt is None:
        raise AssertionError(f"{failure}/{task_kind}: no CEA receipt, HTTP {response.status_code}: {response.text}")
    reason = receipt.get("reason") or {}
    outcome = receipt.get("decision")
    defense = ("capability_registry_owner_conflict" if reason.get("code") == "OWNER_CONFLICT"
               else "review_contract" if reason.get("code") == "IDENTITY_UNVERIFIED_REVIEW_REQUIRED"
               else "qouta_budget" if str(reason.get("code", "")).startswith("BUDGET_")
               else "other:" + str(reason.get("code")))
    result = {"case": failure + "/" + task_kind, "http_status": response.status_code,
              "receipt_outcome": outcome, "receipt_reason": reason,
              "defense": defense,
              "dispatched": (dispatched is not None if task_kind == "duplicate" else None),
              "registry_match": (receipt.get("matched_capability") or {}).get("id"),
              "budget_state": (receipt.get("provider_budget") or {}).get("state")}
    assert result["budget_state"] == "CONSTRAINED", result
    if task_kind == "duplicate":
        assert response.status_code != 201 and not dispatched, result
        assert outcome == "HUMAN_GATE", result
        assert reason.get("code") == "OWNER_CONFLICT", result
        assert result["registry_match"] == capability, result
    else:
        assert response.status_code == 201, result
        assert outcome in ("ALLOW", "REVIEW"), result
        assert reason.get("code") in ("OK", "IDENTITY_UNVERIFIED_REVIEW_REQUIRED"), result
    return result


def run():
    with tempfile.TemporaryDirectory(prefix="crew-428-chaos-") as temp:
        root = Path(temp)
        cases = [run_case(root, failure, kind)
                 for failure in ("missing", "empty", "stale")
                 for kind in ("duplicate", "novel")]
    return {"scenario": "#428 / alfred#51", "credit_class": "plan",
            "sandbox": "temporary HOME, quota, registry and queue; TestClient HTTP; tmux shim; free loopback port",
            "cases": cases, "passed": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    evidence = run()
    rendered = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")
