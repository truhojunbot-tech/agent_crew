"""#927: routing decisions use typed failure evidence, never worker prose."""

import inspect
import json

import pytest
from fastapi.testclient import TestClient

from agent_crew import claude_cloud, instructions, pipeline, server
from agent_crew.failure_kind import FailureKind, classify_provider_record, provider_failure_from_log
from agent_crew.loop import handle_test_result
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import RuntimeTransitionRefused, TaskQueue


def test_failure_routing_does_not_search_summary_findings_or_pane_text():
    assert "has_rate_limit_signal" not in inspect.getsource(server.create_app)
    assert "_pane_has_usage_limit" not in inspect.getsource(server.create_app)
    assert "has_rate_limit_signal" not in inspect.getsource(pipeline.auto_fallback_failed_task)


def test_provider_zero_turn_configuration_needs_structured_error():
    assert classify_provider_record("gemini", {"type": "message",
                                               "content": "invalid model selection"}) is None


def test_cloud_retry_does_not_classify_a_reason_string():
    source = inspect.getsource(claude_cloud.dispatch_cloud_for_role)
    assert '"cloud_submit_timeout:" in reason' not in source
    assert 'launch_error.startswith("cloud_submit_timeout:")' not in source


def test_prose_does_not_become_provider_limit():
    result = TaskResult("t", "failed", "Please add rate-limit handling",
                        findings=["quota exceeded in a documentation example"])
    assert result.error_info is None


@pytest.mark.parametrize("kind,expected_gate", [(None, False), ("provider_limit", True)])
def test_exhaustion_gate_uses_kind_even_when_prose_is_identical(
        tmp_path, monkeypatch, kind, expected_gate):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest("quota-task", "implement", "work", branch="fix/probe",
                              context={"retry_attempt": 2, "risk_tier": 1}))
    assert queue.dequeue(role="implementer", agent="codex") is not None
    app = server.create_app(db, pane_map={}, watchdog_disabled=True,
                            anomaly_disabled=True, identity_required=False,
                            fallback_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks/quota-task/result", json={
            "task_id": "quota-task", "status": "failed",
            "summary": "quota exhausted", "findings": ["rate limit exceeded"],
            "error_info": {"kind": kind} if kind else None,
        })
    assert response.status_code == 200, response.text
    assert bool(queue.get_task("quota-task").context.get("provider_limit_escalation")) == expected_gate


def test_test_findings_do_not_classify_a_failure():
    prose = TaskResult("test-prose", "failed", "test failed",
                       findings=["new failure: tests/test_a.py"])
    typed = TaskResult("test-typed", "failed", "test failed",
                       error_info={"kind": "test_failure"})
    assert handle_test_result(prose) == "not_run"
    assert handle_test_result(typed) == "failed"


def test_llm_tester_protocol_requires_typed_test_failure():
    prompt = instructions.generate("tester", "demo", 8105, agent="gemini",
                                   delivery="dispatcher")
    assert '"kind":"test_failure"' in prompt
    assert '"status":"failed"' in prompt
    assert "new failing tests" in prompt


def test_provider_adapter_classifies_only_structured_fields(tmp_path):
    log = tmp_path / "dispatch.log"
    log.write_text(
        "Please add rate-limit handling to the documentation.\n"
        + json.dumps({"type": "result", "is_error": True,
                      "api_error_status": 429, "result": "arbitrary prose"}) + "\n"
    )
    failure = provider_failure_from_log(str(log), agent="claude")
    assert failure == {"kind": FailureKind.TRANSIENT.value,
                       "provider_code": "http_429", "retry_after": None}
    log.write_text("Please add rate-limit handling to the documentation.\n")
    assert provider_failure_from_log(str(log), agent="claude") is None


def test_zero_turn_error_uses_structured_status_not_error_message():
    record = {"status": "ERROR", "num_turns": 0, "reason": "INVALID_MODEL",
              "error": "something unrelated"}
    assert classify_provider_record("gemini", record)["kind"] == FailureKind.CONFIG_ERROR.value
    assert classify_provider_record("gemini", {"status": "ERROR",
                                               "num_turns": 0})["kind"] == "agent_failed"
    assert classify_provider_record("gemini", {"status": "ERROR", "num_turns": 0,
                                               "error": "invalid model selection"})["kind"] == "config_error"
    assert classify_provider_record("gemini", {"status": "SUCCESS",
                                                  "num_turns": 1,
                                                  "result": "invalid model selection"}) is None


def test_specific_provider_limit_survives_generic_terminal_event(tmp_path):
    path = tmp_path / "dispatch.log"
    path.write_text(
        json.dumps({"status": "ERROR", "reason": "QUOTA_EXHAUSTED"}) + "\n"
        + json.dumps({"status": "ERROR", "error": "no machine code"}) + "\n")
    assert provider_failure_from_log(str(path), agent="gemini")["kind"] == "provider_limit"


def test_structured_transient_survives_zero_turn_generic_terminal_event(tmp_path):
    path = tmp_path / "dispatch.log"
    path.write_text(
        json.dumps({"status": "ERROR", "reason": "UNAVAILABLE"}) + "\n"
        + json.dumps({"status": "ERROR", "num_turns": 0}) + "\n")
    assert provider_failure_from_log(str(path), agent="gemini")["kind"] == "transient"


def test_drain_quarantine_is_recorded_as_typed_trigger(tmp_path):
    q = TaskQueue(str(tmp_path / "tasks.db"))
    q.transition_runtime_state("DRAINING", who="operator:alfred",
                               reason="quarantine is a word in this task")
    assert q.runtime_state_events(limit=1)[0]["trigger_kind"] == "manual"
    q.transition_runtime_state("ACTIVE", who="operator:alfred")
    q.transition_runtime_state("DRAINING", who="operator:alfred",
                               trigger_kind="quarantine", reason="routine")
    assert q.runtime_state_events(limit=1)[0]["trigger_kind"] == "quarantine"
    with pytest.raises(RuntimeTransitionRefused):
        q.transition_runtime_state("ACTIVE", who="operator:alfred")


def test_legacy_drain_without_trigger_kind_cannot_self_undrain(tmp_path):
    q = TaskQueue(str(tmp_path / "tasks.db"))
    q.transition_runtime_state("DRAINING", who="operator:alfred")
    conn = q._connect()
    try:
        conn.execute(
            "INSERT INTO runtime_state_events (epoch, from_state, to_state, direction, "
            "who, reason, created_at) VALUES (1, 'ACTIVE', 'DRAINING', 'tighten', "
            "'operator:alfred', 'legacy row', 1)")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RuntimeTransitionRefused):
        q.transition_runtime_state("ACTIVE", who="operator:alfred")
