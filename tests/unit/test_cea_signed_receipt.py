"""Dispatch grants must survive a writable queue database adversary."""
import json
import time
import uuid
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_crew.cea import signed_receipt
from agent_crew.cea.engine import EngineConfig
from agent_crew.queue import TaskQueue


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
