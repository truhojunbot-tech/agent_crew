"""#342 must observe external economics policy without enforcing it."""
import json

from agent_crew.cli import _tokenomics_policy_path
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


def test_quota_core_v1_report_selects_the_matching_task_decision(monkeypatch, tmp_path, tmp_db):
    policy = tmp_path / "quota-core-v1.json"
    policy.write_text(json.dumps({
        "contract_version": "1.0", "mode": "shadow", "decision_count": 1,
        "decisions": [{"task_id": "quota-v1", "risk_tier": "routine",
                       "recommended_provider_tier": "preserve_current"}],
    }))
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    queue = TaskQueue(tmp_db)

    queue.enqueue(TaskRequest("quota-v1", "implement", "organic-shaped", branch="b"))

    receipt = queue.get_tokenomics_shadow_receipt("quota-v1")
    assert receipt["decision_source"] == "quota_core_contract"
    assert receipt["policy_version"] == "1.0"
    assert json.loads(receipt["recommendation_json"])["risk_tier"] == "routine"


def test_project_policy_path_is_durable_and_operator_overridable(tmp_path):
    default = _tokenomics_policy_path(str(tmp_path), {})
    assert default == str(tmp_path / "tokenomics-policy.json")
    assert _tokenomics_policy_path(str(tmp_path), {"tokenomics_policy_path": "/contract/v1.json"}) == "/contract/v1.json"


def test_unsupported_or_task_missing_contracts_remain_baseline_with_distinct_reason(monkeypatch, tmp_path, tmp_db):
    policy = tmp_path / "unsupported.json"
    policy.write_text(json.dumps({"contract_version": "9.9", "mode": "shadow", "decisions": []}))
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("unsupported", "implement", "x"))
    unsupported = queue.get_tokenomics_shadow_receipt("unsupported")
    assert json.loads(unsupported["recommendation_json"]) is None
    assert json.loads(unsupported["actual_execution_json"])["shadow_reason"] == "contract_version_unsupported"

    policy.write_text(json.dumps({"contract_version": "1.0", "mode": "shadow", "decisions": []}))
    queue.enqueue(TaskRequest("missing-decision", "implement", "x"))
    missing = queue.get_tokenomics_shadow_receipt("missing-decision")
    assert missing["decision_source"] == "baseline"
    assert json.loads(missing["actual_execution_json"])["shadow_reason"] == "task_decision_unavailable"


def test_late_provider_telemetry_refreshes_counterfactual_economics(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("late-usage", "implement", "x"))
    queue.record_attribution("late-usage")
    queue.submit_result("late-usage", TaskResult(task_id="late-usage", status="completed", summary="done"))
    queue.record_task_telemetry("late-usage", TaskTelemetry(output_tokens=19))
    receipt = queue.get_tokenomics_shadow_receipt("late-usage")
    assert json.loads(receipt["economics_json"])["output_tokens"] == 19


def test_completion_refresh_preserves_admission_receipt_and_records_later_decision(
        monkeypatch, tmp_path, tmp_db):
    """A report published after admission must not rewrite what admission knew."""
    policy = tmp_path / "quota-core-v1.json"
    policy.write_text(json.dumps({
        "contract_version": "1.0", "mode": "shadow", "decisions": [],
    }))
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("completion-refresh", "implement", "organic-shaped", branch="b"))
    admission = queue.get_tokenomics_shadow_receipt("completion-refresh")
    assert admission["decision_source"] == "baseline"
    assert admission["policy_version"] is None
    assert json.loads(admission["recommendation_json"]) is None

    # This is a later quota-core publication, not an admission-time lookup.
    policy.write_text(json.dumps({
        "contract_version": "1.0", "mode": "shadow", "decisions": [{
            "task_id": "completion-refresh", "risk_tier": "routine",
        }],
    }))
    queue.record_attribution("completion-refresh")
    queue.submit_result("completion-refresh", TaskResult(
        task_id="completion-refresh", status="completed", summary="done"))

    receipt = queue.get_tokenomics_shadow_receipt("completion-refresh")
    # Immutable admission fact.
    assert receipt["decision_source"] == "baseline"
    assert receipt["policy_version"] is None
    assert json.loads(receipt["recommendation_json"]) is None
    # Separately-provenanced completion fact.
    assert receipt["shadow_decision_source"] == "quota_core_contract"
    assert receipt["shadow_policy_version"] == "1.0"
    assert json.loads(receipt["shadow_recommendation_json"])["risk_tier"] == "routine"
    assert receipt["shadow_contract_sha"]
    assert receipt["shadow_resolved_at"] is not None


def test_completion_refresh_marks_missing_or_corrupt_contract_baseline(monkeypatch, tmp_path, tmp_db):
    policy = tmp_path / "policy.json"
    policy.write_text("not json")
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("completion-baseline", "implement", "x"))
    queue.record_attribution("completion-baseline")
    queue.submit_result("completion-baseline", TaskResult(
        task_id="completion-baseline", status="completed", summary="done"))
    receipt = queue.get_tokenomics_shadow_receipt("completion-baseline")
    assert receipt["shadow_decision_source"] == "baseline"
    assert receipt["shadow_recommendation_json"] is None
    assert receipt["shadow_reason"] == "policy_unavailable"

    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(tmp_path / "missing.json"))
    queue.enqueue(TaskRequest("completion-missing", "implement", "x"))
    queue.record_attribution("completion-missing")
    queue.submit_result("completion-missing", TaskResult(
        task_id="completion-missing", status="completed", summary="done"))
    missing = queue.get_tokenomics_shadow_receipt("completion-missing")
    assert missing["shadow_decision_source"] == "baseline"
    assert missing["shadow_recommendation_json"] is None
    assert missing["shadow_reason"] == "policy_unavailable"
    assert missing["shadow_contract_sha"] is None


def test_completion_refresh_failure_cannot_fail_submitted_result(monkeypatch, tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("completion-exception", "implement", "x"))
    queue.record_attribution("completion-exception")
    monkeypatch.setattr(queue, "_refresh_shadow_after_commit",
                        lambda *_args: (_ for _ in ()).throw(RuntimeError("shadow down")))

    queue.submit_result("completion-exception", TaskResult(
        task_id="completion-exception", status="completed", summary="done"))

    assert queue.get_task_status("completion-exception") == "completed"


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
