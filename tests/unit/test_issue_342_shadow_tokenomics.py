"""#342 must observe external economics policy without enforcing it."""
import json

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.telemetry import TaskTelemetry


def test_unavailable_contract_records_baseline_and_never_blocks(monkeypatch, tmp_db):
    monkeypatch.delenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", raising=False)
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("shadow-1", "implement", "change core", branch="b"))
    receipt = queue.get_tokenomics_shadow_receipt("shadow-1")
    assert receipt["decision_source"] == "baseline"
    assert json.loads(receipt["actual_execution_json"])["cascade"] == "baseline"


def test_unexpected_shadow_failure_cannot_block_enqueue(monkeypatch, tmp_db):
    monkeypatch.setattr("agent_crew.queue.shadow_recommendation",
                        lambda _task: (_ for _ in ()).throw(RuntimeError("shadow down")))
    queue = TaskQueue(tmp_db)
    assert queue.enqueue(TaskRequest("shadow-exception", "implement", "x")) == "shadow-exception"


def test_contract_recommendation_is_counterfactual_and_outcome_keeps_economics(monkeypatch, tmp_path, tmp_db):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"version": "80.1", "recommendation": {"tier": 0, "test": "skip"}}))
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("shadow-2", "implement", "change", branch="b"))
    queue.record_attribution("shadow-2")
    queue.submit_result("shadow-2", TaskResult(task_id="shadow-2", status="completed", summary="done"))
    receipt = queue.get_tokenomics_shadow_receipt("shadow-2")
    assert receipt["decision_source"] == "quota_core_contract"
    assert json.loads(receipt["recommendation_json"])["test"] == "skip"
    assert json.loads(receipt["actual_execution_json"])["cascade"] == "baseline"
    assert receipt["outcome"] == "completed"


def test_late_provider_telemetry_refreshes_counterfactual_economics(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("late-usage", "implement", "x"))
    queue.record_attribution("late-usage")
    queue.submit_result("late-usage", TaskResult(task_id="late-usage", status="completed", summary="done"))
    queue.record_task_telemetry("late-usage", TaskTelemetry(output_tokens=19))
    receipt = queue.get_tokenomics_shadow_receipt("late-usage")
    assert json.loads(receipt["economics_json"])["output_tokens"] == 19


def test_cost_summary_keeps_unreported_usage_unknown(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("known", "implement", "x", context={"issue": 342}))
    queue.enqueue(TaskRequest("unknown", "implement", "x", context={"issue": 342}))
    queue.record_attribution("known")
    queue.record_task_telemetry("known", TaskTelemetry(output_tokens=7))
    summary = queue.token_cost_summary()
    assert summary["total_tokens"] == 7
    assert summary["unobserved_tasks"] == 1
    assert summary["by_issue"]["342"]["total_tokens"] == 7
