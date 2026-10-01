"""Dispatch grants must survive a writable queue database adversary."""
import json
import os
import time
import uuid
import base64
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from agent_crew.cea import signed_receipt
from agent_crew.cea.auth import AdapterIdentity, StaticTokenAuthenticator
from agent_crew.cea.broker import Broker, BROKER_TREE_USER_WRITABLE
from agent_crew.cea.engine import AuthorizationEngine, EngineConfig
from agent_crew.cea.intent import CallerProvenance
from agent_crew.cea.service import encode_intent
from agent_crew.queue import TaskQueue, intent_for_task
from tests.unit.test_sev0_cea_s2c_writer_callsites import WIRED, admitted, task


def _grant(task_id="task-1", description="review code", context=None):
    key = Ed25519PrivateKey.generate()
    context = context or {}
    receipt = {"task_id": task_id, "receipt_id": str(uuid.uuid4()),
               "decision": "ALLOW", "provenance": {}}
    digest = signed_receipt.payload_hash(task_type="review", branch="main",
        description=description, context=context)
    return signed_receipt.sign(receipt, key, payload=digest, build_commit="abc123"), key, digest


def test_signed_grant_rejects_copy_modified_payload_bad_key_and_expiry():
    receipt, key, digest = _grant()
    public = key.public_key()
    check = lambda r, task="task-1", payload=digest, pub=public, now=None: signed_receipt.verify(
        r, pub, task_id=task, receipt_id=r["receipt_id"], payload=payload, now=now)[0]
    assert check(receipt)
    assert not check(receipt, task="task-2")
    assert not check(receipt, payload=signed_receipt.payload_hash(
        task_type="review", branch="main", description="changed", context={}))
    assert not check(receipt, pub=Ed25519PrivateKey.generate().public_key())
    assert not check(dict(receipt, description="tampered"))
    assert not check(receipt, now=time.time() + 3601)


def test_forged_receipt_id_and_unsigned_claim_fail_closed_in_enforce(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"), cea_config=EngineConfig(mode="enforce"))
    conn = queue._connect()
    try:
        for task_id, receipt_id in (("forged", "not-a-real-receipt-id"),
                                    ("unsigned", str(uuid.uuid4()))):
            conn.execute("INSERT INTO tasks (task_id,task_type,description,branch,context,created_at,project,receipt_id) "
                         "VALUES (?,?,?,?,?,?,?,?)", (task_id, "review", "review code", "main",
                                                  json.dumps({}), time.time(), "sandbox", receipt_id))
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        claimed, _ = queue.claim_through_gate(conn, "forged")
        assert not claimed
        conn.rollback()
        conn.execute("BEGIN IMMEDIATE")
        claimed, _ = queue.claim_through_gate(conn, "unsigned")
        assert not claimed
        conn.rollback()
    finally:
        conn.close()


def test_test_mode_records_unsigned_check_without_refusing(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"), cea_config=EngineConfig(mode="test"))
    monkeypatch.setattr(queue, "_cea_claim_gate", lambda *a, **k: None)
    conn = queue._connect()
    try:
        conn.execute("INSERT INTO tasks (task_id,task_type,description,branch,context,created_at,project,receipt_id) "
                     "VALUES (?,?,?,?,?,?,?,?)", ("test-task", "review", "review code", "main",
                                              "{}", time.time(), "sandbox", "forged"))
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        claimed, _ = queue.claim_through_gate(conn, "test-task")
        # The pre-existing NO_RECEIPT gate still decides this fabricated row.
        assert not claimed
        conn.commit()
        row = conn.execute("SELECT fields FROM task_exec_events WHERE task_id=? AND event=?",
                           ("test-task", "signed_receipt_check")).fetchone()
        assert json.loads(row[0])["outcome"] == "REFUSED_UNSIGNED_RECEIPT"
    finally:
        conn.close()


def test_claim_consumes_signed_nonce_and_refuses_copy_or_modified_description(tmp_path, monkeypatch):
    receipt, key, _ = _grant()
    queue = TaskQueue(str(tmp_path / "tasks.db"), cea_config=EngineConfig(mode="enforce"))
    monkeypatch.setattr(signed_receipt, "load_public", lambda path: key.public_key())
    monkeypatch.setattr(queue, "_cea_receipt_for_task_on",
                        lambda conn, task_id: (receipt["receipt_id"], receipt))
    monkeypatch.setattr(queue, "_cea_claim_gate", lambda *a, **k:
                        SimpleNamespace(proceed=True, receipt_id=receipt["receipt_id"],
                                        outcome=SimpleNamespace(value="PROCEED")))
    monkeypatch.setattr(queue, "_cea_transition_in_txn", lambda *a, **k: None)
    conn = queue._connect()
    try:
        for task_id, description in (("task-1", "review code"), ("task-2", "review code"),
                                     ("modified", "changed")):
            conn.execute("INSERT INTO tasks (task_id,task_type,description,branch,context,created_at,project,receipt_id) "
                         "VALUES (?,?,?,?,?,?,?,?)", (task_id, "review", description, "main", "{}",
                                                  time.time(), "sandbox", receipt["receipt_id"]))
        conn.commit()
        for task_id in ("task-2", "modified"):
            conn.execute("BEGIN IMMEDIATE")
            assert queue.claim_through_gate(conn, task_id)[0] is False
            conn.rollback()
        conn.execute("UPDATE tasks SET description='changed' WHERE task_id='task-1'")
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        assert queue.claim_through_gate(conn, "task-1")[0] is False
        conn.rollback()
        conn.execute("UPDATE tasks SET description='review code' WHERE task_id='task-1'")
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        assert queue.claim_through_gate(conn, "task-1")[0] is True
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        assert queue.claim_through_gate(conn, "task-1")[0] is False
        conn.rollback()
    finally:
        conn.close()


def test_staged_receipt_verification_requires_matching_decision_and_advisory_code():
    receipt, key, digest = _grant()
    receipt = signed_receipt.sign(dict(receipt, decision="REVIEW",
        reason={"code": "IDENTITY_UNVERIFIED_REVIEW_REQUIRED"}), key,
        payload=digest, build_commit="abc123")
    check = lambda r, codes: signed_receipt.verify(r, key.public_key(), task_id="task-1",
        receipt_id=r["receipt_id"], payload=digest, enforce_codes=codes)[0]
    assert check(receipt, frozenset({"RUNTIME_STATE_FORBIDS"}))
    assert not check(receipt, None)
    assert not check(receipt, frozenset({"IDENTITY_UNVERIFIED_REVIEW_REQUIRED"}))
    enforced = signed_receipt.sign(dict(receipt, decision="BLOCK",
        reason={"code": "RUNTIME_STATE_FORBIDS"}), key,
        payload=digest, build_commit="abc123")
    assert not check(enforced, frozenset({"RUNTIME_STATE_FORBIDS"}))
    wrong_binding = dict(receipt, provenance={**receipt["provenance"],
        signed_receipt.DISPATCH_BINDING: {**receipt["provenance"][signed_receipt.DISPATCH_BINDING],
                                          "decision": "ALLOW"}})
    wrong_binding["signature"] = {**wrong_binding["signature"], "value": base64.b64encode(
        key.sign(signed_receipt._canonical({k: v for k, v in wrong_binding.items()
                                            if k != "signature"}))).decode("ascii")}
    assert signed_receipt.verify_signature(wrong_binding, key.public_key())
    assert not check(wrong_binding, frozenset({"RUNTIME_STATE_FORBIDS"}))


def test_post_commit_risk_telemetry_keeps_signed_payload_stable():
    baseline = signed_receipt.payload_hash(task_type="implement", branch="main",
        description="review code", context={"authority_decision_ids": ["T0"]})
    after = signed_receipt.payload_hash(task_type="implement", branch="main",
        description="review code", context={"authority_decision_ids": ["T0"],
            "risk_declaration": {"bounded_routine_fix": True},
            "risk_tier_shadow": [{"tier": "T2"}]})
    assert after == baseline
    assert signed_receipt.payload_hash(task_type="implement", branch="main",
        description="changed", context={"authority_decision_ids": ["T0"]}) != baseline


@pytest.mark.parametrize("code,codes,claimed", [
    ("IDENTITY_UNVERIFIED_REVIEW_REQUIRED", frozenset({"RUNTIME_STATE_FORBIDS"}), True),
    ("RUNTIME_STATE_FORBIDS", frozenset({"RUNTIME_STATE_FORBIDS"}), False),
    ("IDENTITY_UNVERIFIED_REVIEW_REQUIRED", None, False),
])
def test_claim_gate_staged_and_full_enforce_decisions(tmp_path, monkeypatch, code, codes, claimed):
    receipt, key, digest = _grant()
    receipt = signed_receipt.sign(dict(receipt, decision="REVIEW" if claimed or codes is None
        else "BLOCK", reason={"code": code}), key, payload=digest, build_commit="abc123")
    queue = TaskQueue(str(tmp_path / "tasks.db"),
        cea_config=EngineConfig(mode="enforce", enforce_codes=codes))
    monkeypatch.setattr(signed_receipt, "load_public", lambda path: key.public_key())
    monkeypatch.setattr(queue, "_cea_receipt_for_task_on",
        lambda conn, task_id: (receipt["receipt_id"], receipt))
    monkeypatch.setattr(queue, "_cea_claim_gate", lambda *a, **k: SimpleNamespace(
        proceed=True, receipt_id=receipt["receipt_id"],
        outcome=SimpleNamespace(value="PROCEED")))
    monkeypatch.setattr(queue, "_cea_transition_in_txn", lambda *a, **k: None)
    conn = queue._connect()
    try:
        conn.execute("INSERT INTO tasks (task_id,task_type,description,branch,context,created_at,project,receipt_id) "
            "VALUES (?,?,?,?,?,?,?,?)", ("task-1", "review", "review code", "main", "{}",
                time.time(), "sandbox", receipt["receipt_id"]))
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        assert queue.claim_through_gate(conn, "task-1")[0] is claimed
        conn.commit() if claimed else conn.rollback()
    finally:
        conn.close()


def test_broker_signed_enqueue_and_staged_claim_with_postcommit_telemetry(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    private_path = tmp_path / "signing.key"
    private_path.write_bytes(key.private_bytes(serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw, serialization.NoEncryption()))
    build_path = tmp_path / "SRC_COMMIT"
    build_path.write_text("abc123")
    monkeypatch.setenv("AGENT_CREW_CEA_RECEIPT_SIGNING_KEY_FILE", str(private_path))
    monkeypatch.setenv("AGENT_CREW_AUTHZ_SRC_COMMIT_PATH", str(build_path))
    monkeypatch.setattr(signed_receipt, "load_public", lambda path: key.public_key())
    config = EngineConfig(mode="enforce", enforce_codes=frozenset({"RUNTIME_STATE_FORBIDS"}))
    queue = TaskQueue(str(tmp_path / "tasks.db"), cea_config=config, cea_providers=dict(WIRED))
    engine = AuthorizationEngine(config=EngineConfig(mode="enforce",
        fallback_reason=BROKER_TREE_USER_WRITABLE), **WIRED)
    broker = Broker(str(tmp_path / "socket"), degraded=True, client_uids=(os.geteuid(),),
        integrity_paths=(str(tmp_path),), decision_engine=engine, connect=queue._connect,
        authenticator=StaticTokenAuthenticator({"test-token": AdapterIdentity(
            "test", CallerProvenance.DIRECT)}))

    def authorize(item, *, context=None, **kwargs):
        result = broker._authorize(encode_intent(intent_for_task(item, context=context), "test-token"))
        assert "receipt" in result, result
        return SimpleNamespace(receipt=result["receipt"])

    monkeypatch.setattr(queue, "authorize_task", authorize)
    monkeypatch.setattr(queue, "_cea_claim_gate", lambda *a, **k: SimpleNamespace(
        proceed=True, receipt_id="sandbox", outcome=SimpleNamespace(value="PROCEED")))
    monkeypatch.setattr(queue, "_cea_transition_in_txn", lambda *a, **k: None)
    item = task("staged-review", context=admitted())
    queue.enqueue(item)
    conn = queue._connect()
    try:
        row = conn.execute("SELECT context FROM tasks WHERE task_id=?", (item.task_id,)).fetchone()
        assert "risk_declaration" in json.loads(row["context"])
        conn.execute("BEGIN IMMEDIATE")
        assert queue.claim_through_gate(conn, item.task_id)[0]
        print("SANDBOX staged REVIEW claimed: task=staged-review, telemetry present, signature valid")
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        assert not queue.claim_through_gate(conn, item.task_id)[0]
        conn.rollback()
        conn.execute("UPDATE tasks SET description='tampered' WHERE task_id=?", (item.task_id,))
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        assert not queue.claim_through_gate(conn, item.task_id)[0]
        conn.rollback()
    finally:
        conn.close()
