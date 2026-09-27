"""Opt-in broker boundary and phase-one integrity downgrade."""
from __future__ import annotations

import os
import pytest
from dataclasses import replace

from agent_crew.cea.broker import (
    BROKER_SPAWNED, BROKER_TREE_USER_WRITABLE, Broker, Registration,
    writable_broker_tree,
)
from agent_crew.cea.engine import Authorization, EngineConfig, get_engine
from agent_crew.cea.intent import Intent, IntentIdentity, Target, WorkClass
from agent_crew.cea.service import BrokerDecisionClient


def test_broker_socket_selects_client_only_when_opted_in():
    config = EngineConfig.from_env({"AGENT_CREW_CEA_BROKER_SOCKET": "/tmp/example.sock"})
    assert config.broker_socket == "/tmp/example.sock"
    assert isinstance(get_engine(config=config), BrokerDecisionClient)
    assert not isinstance(get_engine(config=replace(config, broker_socket=None)), BrokerDecisionClient)


@pytest.mark.parametrize("mode", ["enforce", "shadow", "test"])
def test_unreachable_broker_respects_embedded_mode(monkeypatch, mode):
    observed = []

    class Embedded:
        def __init__(self, *, config, **providers):
            observed.append(config)

        def authorize(self, conn, intent, caller, *, retry=False):
            observed.append("authorize")
            return Authorization(receipt={"downgrade_reason": observed[0].fallback_reason},
                                 http_status=409, code="EMBEDDED_ENFORCE_FORBIDDEN")

        def refuse(self, conn, intent, caller, *, code, text):
            observed.append("refuse")
            return Authorization(receipt={"decision": "BLOCK", "reason": {"code": code},
                                          "signature": {"status": "UNVERIFIED"}},
                                 http_status=403, code=code)

    monkeypatch.setattr("agent_crew.cea.service.AuthorizationEngine", Embedded)
    client = BrokerDecisionClient(EngineConfig(mode=mode, broker_socket="/missing.sock"))
    monkeypatch.setattr(client.broker, "decision", lambda request: {"error": "broker_unreachable"})
    intent = Intent(identity=IntentIdentity(project="alfred", work_class=WorkClass.IMPLEMENT,
                    target=Target(repo="example/alfred", base_ref="main", scope_anchors=())),
                    task_id="t", task_type="implement", description="test")
    result = client.authorize(None, intent, object())
    assert observed[0].mode == mode and observed[0].broker_socket is None
    if mode == "enforce":
        assert observed[1] == "refuse"
        assert result.http_status == 403 and result.code == "BROKER_UNREACHABLE"
        assert result.receipt["signature"]["status"] != "VERIFIED"
    else:
        assert observed[1] == "authorize"
        assert result.receipt["downgrade_reason"] == "broker_unreachable"


@pytest.mark.parametrize("broker_error", ["broker_unreachable", "backend_failed"])
def test_enforce_broker_outage_records_unsigned_refusal(monkeypatch, broker_error):
    import sqlite3
    from agent_crew.cea.auth import in_process_caller
    from agent_crew.cea import store

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    client = BrokerDecisionClient(EngineConfig(mode="enforce", broker_socket="/missing.sock"))
    monkeypatch.setattr(client.broker, "decision", lambda request: {"error": broker_error})
    intent = Intent(identity=IntentIdentity(project="alfred", work_class=WorkClass.IMPLEMENT,
                    target=Target(repo="example/alfred", base_ref="main", scope_anchors=())),
                    task_id="outage", task_type="implement", description="test")
    auth = client.authorize(conn, intent, in_process_caller())
    assert auth.http_status == 403 and auth.code == "BROKER_UNREACHABLE"
    assert auth.receipt["decision"] == "BLOCK"
    assert auth.receipt["signature"]["status"] == "UNVERIFIED"
    assert store.current_receipt(conn, auth.receipt["receipt_id"])["reason"]["code"] == "BROKER_UNREACHABLE"


def test_enforce_broker_outage_enqueue_returns_403_reason(tmp_path, monkeypatch):
    from agent_crew.cea.auth import in_process_caller
    from agent_crew.cea.refusal_http import refusal_payload
    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import AdmissionRefused, TaskQueue

    config = EngineConfig(mode="enforce", broker_socket="/missing.sock")
    client = BrokerDecisionClient(config)
    monkeypatch.setattr(client.broker, "decision", lambda request: {"error": "broker_unreachable"})
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    task = TaskRequest(task_id="outage", task_type="implement", description="test",
                       project="alfred")
    intent = Intent(identity=IntentIdentity(project="alfred", work_class=WorkClass.IMPLEMENT,
                    target=Target(repo="alfred", base_ref="main", scope_anchors=())),
                    task_id="outage", task_type="implement", description="test")
    conn = queue._connect()
    try:
        receipt = client.authorize(conn, intent, in_process_caller()).receipt
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(queue, "cea_engine", lambda *args: client)
    monkeypatch.setattr(queue, "cea_config", lambda *args: config)
    with pytest.raises(AdmissionRefused) as exc:
        queue.enqueue_with_receipt(task, receipt)
    status, body = refusal_payload(exc.value)
    assert status == 403 and body["reason"].startswith("BROKER_UNREACHABLE:")
    assert queue.get_task_status("outage") is None


def test_user_writable_ancestor_prevents_verified(tmp_path):
    launcher = tmp_path / "broker-launch.sh"
    launcher.write_text("#!/bin/sh\n")
    assert writable_broker_tree(str(launcher))
    b = Broker(str(tmp_path), integrity_paths=(str(launcher),))
    reg = Registration(7, 9, "r", 1, 6, "nonce", origin=BROKER_SPAWNED)
    result = b._attest_broker_spawned(reg, {"ok": True})
    assert result["executor_binding_status"] == "UNVERIFIED"
    assert result["downgrade_reason"] == BROKER_TREE_USER_WRITABLE
    assert b.handle(os.getpid(), os.geteuid(), {"op": "status"})["downgrade_reason"] == BROKER_TREE_USER_WRITABLE


def test_broker_remote_decisions_enforce_only_runtime_code(tmp_path, monkeypatch):
    """Sandbox policy: runtime forbids stops; other blocks remain advisory."""
    import sqlite3
    import threading
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("cea_engine_test_fixtures", Path(__file__).with_name("test_sev0_cea_engine.py"))
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    from agent_crew.cea.auth import AdapterIdentity, StaticTokenAuthenticator
    from agent_crew.cea.broker import BrokerClient
    from agent_crew.cea.intent import CallerProvenance
    from agent_crew.cea.runtime_state import RuntimeState
    from agent_crew.cea.service import encode_intent
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    signing = Ed25519PrivateKey.generate()
    private = tmp_path / "receipt-signing.key"
    private.write_bytes(signing.private_bytes(serialization.Encoding.Raw,
                        serialization.PrivateFormat.Raw, serialization.NoEncryption()))
    marker = tmp_path / "SRC_COMMIT"
    marker.write_text("sandbox-build")
    monkeypatch.setenv("AGENT_CREW_CEA_RECEIPT_SIGNING_KEY_FILE", str(private))
    monkeypatch.setenv("AGENT_CREW_AUTHZ_SRC_COMMIT_PATH", str(marker))

    db = tmp_path / "receipts.db"
    token = "sandbox-token"
    auth = StaticTokenAuthenticator({token: AdapterIdentity("cron:sandbox", CallerProvenance.CRON)})
    state = [RuntimeState.STOPPED]

    class Runtime:
        def current(self):
            return fixtures.FakeRuntime(state[0]).current()

    eng = fixtures.engine(config=EngineConfig(mode="enforce", fallback_reason=BROKER_TREE_USER_WRITABLE),
                          runtime=Runtime())
    sockdir = tmp_path / "sock"
    sockdir.mkdir(mode=0o710)
    broker = Broker(str(sockdir), degraded=True, client_uids=(os.geteuid(),),
                    integrity_paths=(str(tmp_path),), decision_engine=eng,
                    connect=lambda: sqlite3.connect(str(db)), authenticator=auth)
    broker.bind()
    assert os.lstat(broker.sock_path).st_gid == os.lstat(sockdir).st_gid
    thread = threading.Thread(target=broker.serve_forever, daemon=True)
    thread.start()
    client = BrokerClient(broker.sock_path, expected_uid=os.geteuid())
    try:
        blocked = client.decision(encode_intent(fixtures.intent("runtime-block"), token))
        assert blocked["code"] == "RUNTIME_STATE_FORBIDS"
        assert blocked["receipt"]["downgrade_reason"] == BROKER_TREE_USER_WRITABLE
        state[0] = RuntimeState.ACTIVE
        allowed = client.decision(encode_intent(fixtures.intent(
            "allowed", ident=fixtures.identity(work_class=fixtures.WorkClass.REVIEW),
            task_type="review"), token))
        assert allowed["code"] == "OK" and allowed["receipt"]["decision"] == "ALLOW"
        state[0] = RuntimeState.ACTIVE
        eng.budgets = fixtures.FakeBudget(fixtures.BudgetClass.EXHAUSTED)
        advisory = client.decision(encode_intent(fixtures.intent("advisory"), token))
        assert advisory["code"] == "BUDGET_EXHAUSTED"
        enforce_codes = frozenset({"RUNTIME_STATE_FORBIDS"})
        assert blocked["code"] in enforce_codes
        assert allowed["code"] == "OK" and allowed["receipt"]["decision"] == "ALLOW"
        assert advisory["code"] not in enforce_codes
    finally:
        broker.close()


def test_bind_assigns_validated_client_gid_before_chmod(tmp_path, monkeypatch):
    from agent_crew.cea import broker as broker_module
    directory = tmp_path / "socket"
    directory.mkdir(mode=0o710)
    alternate = next((gid for gid in os.getgroups() if gid != os.getegid()), None)
    if alternate is None:
        pytest.skip("no supplementary group available for distinct broker/client gid")
    os.chown(directory, -1, alternate)
    broker = Broker(str(directory), degraded=True, client_uids=(os.geteuid(),))
    real_chown = broker_module.os.chown
    calls = []

    def record_chown(path, uid, gid):
        calls.append((path, uid, gid))
        return real_chown(path, uid, gid)

    monkeypatch.setattr(broker_module.os, "chown", record_chown)
    try:
        broker.bind()
        assert calls == [(broker.sock_path, -1, os.lstat(directory).st_gid)]
        assert os.lstat(broker.sock_path).st_gid == os.lstat(directory).st_gid
        assert os.lstat(broker.sock_path).st_gid != os.getegid()
    finally:
        broker.close()


def test_main_wires_authorize_with_same_tree_downgrade_as_attest(tmp_path, monkeypatch, capsys):
    """Exercise the production CLI branch with an isolated database and writable tree."""
    from agent_crew.cea import broker as broker_module
    from agent_crew.cea.auth import DenyAllAuthenticator
    from agent_crew.cea.engine import AuthorizationEngine

    db = tmp_path / "tasks.db"
    db.touch()
    launcher = tmp_path / "broker-launch.sh"
    launcher.write_text("#!/bin/sh\n")
    monkeypatch.setenv("AGENT_CREW_CEA_BROKER_DB", str(db))
    monkeypatch.setenv("AGENT_CREW_AUTHZ_LAUNCHER_PATH", str(launcher))
    monkeypatch.setenv("AGENT_CREW_AUTHZ_SRC_COMMIT_PATH", str(tmp_path / "SRC_COMMIT"))
    monkeypatch.setenv("AGENT_CREW_CEA_REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.setenv("AGENT_CREW_CEA_SNAPSHOT_PATH", str(tmp_path / "snapshot.json"))
    monkeypatch.setenv("AGENT_CREW_CEA_MEMORY_CMD", str(tmp_path / "missing-memory"))
    monkeypatch.setenv("AGENT_CREW_CEA_QUOTA_CACHE_DIR", str(tmp_path / "quota"))
    monkeypatch.delenv("AGENT_CREW_CEA_CALLER_TOKENS", raising=False)
    counts = []
    original = broker_module.writable_broker_tree

    def measured_tree(*paths, **kwargs):
        counts.append((paths, kwargs))
        return original(*paths, **kwargs)

    def inspect_preflight(self):
        assert self.tree_writable
        assert isinstance(self.decision_engine, AuthorizationEngine)
        assert self.decision_engine.config.fallback_reason == BROKER_TREE_USER_WRITABLE
        assert isinstance(self.authenticator, DenyAllAuthenticator)
        assert callable(self.connect)
        assert self.handle(os.getpid(), os.geteuid(), {"op": "status"})["downgrade_reason"] == BROKER_TREE_USER_WRITABLE
        return {"downgrade_reason": BROKER_TREE_USER_WRITABLE}

    monkeypatch.setattr(broker_module, "writable_broker_tree", measured_tree)
    monkeypatch.setattr(Broker, "preflight", inspect_preflight)
    assert broker_module.main(["--sock-dir", str(tmp_path / "sock"), "--degraded", "--check"]) == 0
    assert len(counts) == 1
    assert BROKER_TREE_USER_WRITABLE in capsys.readouterr().out
