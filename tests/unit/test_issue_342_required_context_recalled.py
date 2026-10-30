"""#342(A): required-context recall is nullable evidence, never a guess."""
import json
from pathlib import Path

import jsonschema

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


def test_context_pack_observation_maps_healthy_degraded_and_absent_to_tri_state():
    from types import SimpleNamespace
    from agent_crew.server import _required_context_recalled_observation

    assert _required_context_recalled_observation(
        SimpleNamespace(degraded=False, items=[object()])
    ) is True
    assert _required_context_recalled_observation(SimpleNamespace(degraded=True)) is False
    assert _required_context_recalled_observation(
        SimpleNamespace(degraded=False, items=[])
    ) is None
    assert _required_context_recalled_observation(None) is None


def _completed_receipt(queue, task_id):
    queue.enqueue(TaskRequest(task_id, "implement", "x"))
    queue.record_attribution(task_id)
    queue.submit_result(task_id, TaskResult(task_id, "completed", "done"))
    return queue.get_tokenomics_shadow_receipt(task_id)


def test_context_recall_evidence_preserves_true_false_and_unknown(tmp_db):
    queue = TaskQueue(tmp_db)
    for task_id, observed in (("recall-true", True), ("recall-false", False),
                              ("recall-unknown", None)):
        queue.enqueue(TaskRequest(task_id, "implement", "x"))
        queue.record_attribution(task_id)
        queue.record_required_context_recalled(task_id, observed)
        queue.submit_result(task_id, TaskResult(task_id, "completed", "done"))
        receipt = queue.get_tokenomics_shadow_receipt(task_id)
        assert json.loads(receipt["evidence_json"])["required_context_recalled"] is observed


def test_context_evidence_has_quota_core_required_shape(tmp_db):
    queue = TaskQueue(tmp_db)
    receipt = _completed_receipt(queue, "evidence-shape")
    evidence = json.loads(receipt["evidence_json"])
    assert set(evidence) == {
        "outcome", "independent_review_correct", "required_context_recalled",
        "context_growth_tokens", "retry_of", "fallback_of", "token_observations",
        "risk_declaration",
    }
    assert evidence["required_context_recalled"] is None
    assert evidence["risk_declaration"]["declaration_source"] == "explicit"
    assert set(evidence["token_observations"]) == {
        "uncached_input_tokens", "cache_write_tokens", "cache_read_tokens",
        "output_tokens", "reasoning_tokens",
    }
    # Source: quota-core 4d8aa067b2a6dcd0300a5d75e2e234cc5a890c67.
    schema_path = Path(__file__).resolve().parents[1] / "fixtures/quota_core_4d8aa06_evidence_schema.json"
    schema = json.loads(schema_path.read_text())
    quality_evidence = {key: value for key, value in evidence.items()
                        if key != "risk_declaration"}
    quality_evidence["recall_not_applicable"] = None
    jsonschema.validate(quality_evidence, schema)


def test_context_evidence_failure_is_best_effort(monkeypatch, tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("evidence-best-effort", "implement", "x"))
    queue.record_attribution("evidence-best-effort")
    monkeypatch.setattr(queue, "record_required_context_recalled",
                        lambda *_args: (_ for _ in ()).throw(RuntimeError("telemetry down")))
    # The producer caller will isolate this exception; queue completion itself
    # remains independently safe.
    queue.submit_result("evidence-best-effort", TaskResult(
        "evidence-best-effort", "completed", "done"))
    assert queue.get_attribution("evidence-best-effort")["outcome"] == "completed"
