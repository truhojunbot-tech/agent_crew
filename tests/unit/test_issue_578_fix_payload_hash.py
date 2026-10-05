"""#578: a cascade fix must retain its broker-signed dispatch binding."""

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_crew.cea import signed_receipt
from agent_crew.cea.engine import EngineConfig
from agent_crew.cea.input_providers.snapshot import _canonical
from agent_crew.pipeline import auto_enqueue_fix, auto_enqueue_review, auto_enqueue_test
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


def _broker_payload(task, context):
    """The still deployed broker's stable field set, before #342 telemetry."""
    legacy_volatile = signed_receipt.VOLATILE_CONTEXT - {
        "risk_declaration", "risk_tier_shadow"
    }
    stable = {key: value for key, value in context.items()
              if key not in legacy_volatile}
    body = {"task_type": task.task_type, "branch": task.branch,
            "description": task.description, "context": stable}
    return "sha256:" + hashlib.sha256(_canonical(body)).hexdigest()


def test_auto_fix_stored_row_matches_signed_broker_payload(tmp_db, monkeypatch):
    queue = TaskQueue(tmp_db)
    key = Ed25519PrivateKey.generate()
    queue.enqueue(TaskRequest(
        "risk-root", "implement", "implement feature", branch="feature/risk",
        context={"risk_declaration": {
            "safety_or_live_change": False,
            "broad_architecture_change": False,
            "bounded_routine_fix": True,
            "human_gate_required": False,
            "declaration_source": "explicit", "confidence": "high"}},
    ))
    queue.submit_result("risk-root", TaskResult(
        "risk-root", "completed", "done", pr_number=51))
    review_id = auto_enqueue_review(
        queue, "risk-root", pr_number=51, pr_state_fn=lambda _: "open")
    assert review_id
    queue.submit_result(review_id, TaskResult(
        review_id, "completed", "fix it", verdict="request_changes",
        findings=["Add the missing test"], pr_number=51))

    # The first two stages are fixtures. The fix admission is the enforced one.
    queue._cea_config_override = EngineConfig(mode="enforce")

    authorize = queue.authorize_task
    signed = {}

    def broker_authorize(task, *, context, provenance, retry):
        auth = authorize(task, context=context, provenance=provenance, retry=retry)
        receipt = dict(auth.receipt, decision="ALLOW",
                       reason={"code": "OK", "text": "admitted"})
        signed["admission_context"] = dict(context)
        signed["receipt"] = signed_receipt.sign(
            receipt, key, payload=_broker_payload(task, context),
            build_commit="broker-test-build")
        return replace(auth, receipt=signed["receipt"])

    monkeypatch.setattr(queue, "authorize_task", broker_authorize)
    # The local fixture has no policy snapshot. Keep the admission gate out of
    # this binding test; the queue itself remains in enforce mode.
    from agent_crew import queue as queue_module
    monkeypatch.setattr(queue_module._cea_callsites, "gate_enqueue", lambda *a, **k:
        SimpleNamespace(proceed=True, outcome=SimpleNamespace(value="PROCEED"),
                        as_record=lambda: {"proceed": True}))
    fix_id = auto_enqueue_fix(queue, review_id, pr_state_fn=lambda _: "open",
                              repo="owner/repo")
    assert fix_id
    conn = queue._connect()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (fix_id,)).fetchone()
    finally:
        conn.close()
    payload = signed_receipt.payload_hash(
        task_type=row["task_type"], branch=row["branch"],
        description=row["description"], context=json.loads(row["context"]))
    receipt = signed["receipt"]
    assert "risk_declaration" not in signed["admission_context"]
    assert signed_receipt.verify_signature(receipt, key.public_key())
    assert signed_receipt.verify(
        receipt, key.public_key(), task_id=fix_id, payload=payload,
        receipt_id=receipt["receipt_id"])[0]
    assert queue.get_task_context(fix_id)["risk_declaration"]["inherited_from"] == "risk-root"


@pytest.mark.parametrize("successor", ["review", "test"])
def test_review_and_test_successors_match_signed_broker_payload(
        tmp_db, monkeypatch, successor):
    queue = TaskQueue(tmp_db)
    key = Ed25519PrivateKey.generate()
    queue.enqueue(TaskRequest(
        "risk-root", "implement", "implement feature", branch="feature/risk",
        context={"risk_declaration": {
            "safety_or_live_change": False, "broad_architecture_change": False,
            "bounded_routine_fix": True, "human_gate_required": False,
            "declaration_source": "explicit", "confidence": "high"}},
    ))
    queue.submit_result("risk-root", TaskResult(
        "risk-root", "completed", "done", pr_number=51))
    review_id = None
    if successor == "test":
        review_id = auto_enqueue_review(
            queue, "risk-root", pr_number=51, pr_state_fn=lambda _: "open")
        assert review_id
        queue.submit_result(review_id, TaskResult(
            review_id, "completed", "approved", verdict="approve",
            findings=[], pr_number=51))

    queue._cea_config_override = EngineConfig(mode="enforce")
    authorize = queue.authorize_task
    signed = {}

    def broker_authorize(task, *, context, provenance, retry):
        auth = authorize(task, context=context, provenance=provenance, retry=retry)
        receipt = dict(auth.receipt, decision="ALLOW",
                       reason={"code": "OK", "text": "admitted"})
        signed["admission_context"] = dict(context)
        signed["receipt"] = signed_receipt.sign(
            receipt, key, payload=_broker_payload(task, context),
            build_commit="broker-test-build")
        return replace(auth, receipt=signed["receipt"])

    monkeypatch.setattr(queue, "authorize_task", broker_authorize)
    from agent_crew import queue as queue_module
    monkeypatch.setattr(queue_module._cea_callsites, "gate_enqueue", lambda *a, **k:
        SimpleNamespace(proceed=True, outcome=SimpleNamespace(value="PROCEED"),
                        as_record=lambda: {"proceed": True}))
    successor_id = (auto_enqueue_review(
        queue, "risk-root", pr_number=51, pr_state_fn=lambda _: "open")
        if successor == "review" else auto_enqueue_test(
            queue, review_id, pr_state_fn=lambda _: "open"))
    assert successor_id
    conn = queue._connect()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (successor_id,)).fetchone()
    finally:
        conn.close()
    context = json.loads(row["context"])
    assert "risk_declaration" not in signed["admission_context"]
    payload = signed_receipt.payload_hash(
        task_type=row["task_type"], branch=row["branch"],
        description=row["description"], context=context)
    receipt = signed["receipt"]
    assert signed_receipt.verify_signature(receipt, key.public_key())
    assert signed_receipt.verify(
        receipt, key.public_key(), task_id=successor_id, payload=payload,
        receipt_id=receipt["receipt_id"])[0]
    assert context["risk_declaration"]["inherited_from"] == "risk-root"
