"""Stage ENFORCE by reason code without changing receipt verdicts."""
import logging

import pytest

from agent_crew.cea.callsites import _gate
from agent_crew.cea.engine import EngineConfig, resolve_enforce_codes
from agent_crew.cea.validator import ValidationOutcome, ValidationPoint, ValidationResult
from agent_crew.queue import TaskQueue
from tests.unit.test_sev0_cea_s2c_writer_callsites import WIRED, admitted, task


def _verdict(code, outcome, config):
    result = ValidationResult(
        point=ValidationPoint.ENQUEUE, outcome=outcome,
        reason=("DECISION_HUMAN_GATE" if outcome is ValidationOutcome.HUMAN_GATE
                else "DECISION_BLOCK") + ": receipt decision", receipt_id="receipt-1")
    receipt = {"reason": {"code": code}}
    return _gate(ValidationPoint.ENQUEUE, result, config, receipt)


@pytest.mark.parametrize("mode", ["enforce", "test"])
def test_only_listed_reason_stops_work_and_receipt_verdict_is_preserved(mode):
    config = EngineConfig(mode=mode, enforce_codes=frozenset({"RUNTIME_STATE_FORBIDS"}))
    denied = _verdict("RUNTIME_STATE_FORBIDS", ValidationOutcome.BLOCK, config)
    assert denied.proceed is False and denied.enforced is True
    for code, outcome in (
        ("ALREADY_COMPLETED", ValidationOutcome.BLOCK),
        ("OWNER_CONFLICT", ValidationOutcome.HUMAN_GATE),
        ("DUPLICATE_INTENT", ValidationOutcome.BLOCK),
    ):
        gate = _verdict(code, outcome, config)
        assert gate.proceed is True and gate.enforced is False
        assert gate.outcome is outcome
        assert gate.as_record()["advisory"] is True
        assert gate.as_record()["review_required"] is True


def test_unset_allowlist_preserves_enforce_and_shadow_behavior():
    for mode, stops in (("enforce", True), ("shadow", False)):
        gate = _verdict("ALREADY_COMPLETED", ValidationOutcome.BLOCK,
                        EngineConfig(mode=mode))
        assert gate.proceed is not stops
        assert gate.enforced is stops
    shadow_with_list = _verdict(
        "RUNTIME_STATE_FORBIDS", ValidationOutcome.BLOCK,
        EngineConfig(mode="shadow", enforce_codes=frozenset({"RUNTIME_STATE_FORBIDS"})))
    assert shadow_with_list.proceed is True and shadow_with_list.enforced is False
    assert "review_required" not in shadow_with_list.as_record()


def test_per_project_precedence_and_unknown_code_warning(caplog):
    env = {
        "AGENT_CREW_CEA_MODE": "enforce",
        "AGENT_CREW_CEA_ENFORCE_CODES": "ALREADY_COMPLETED",
        "AGENT_CREW_CEA_ENFORCE_CODES_ALFRED": "RUNTIME_STATE_FORBIDS, TYPO_CODE",
    }
    with caplog.at_level(logging.WARNING):
        hot = EngineConfig.from_env(env, "alfred")
    cold = EngineConfig.from_env(env, "other")
    assert hot.enforce_codes == frozenset({"RUNTIME_STATE_FORBIDS"})
    assert cold.enforce_codes == frozenset({"ALREADY_COMPLETED"})
    assert "TYPO_CODE" in caplog.text
    assert resolve_enforce_codes({}, "alfred") is None


def test_advisory_start_response_says_go_and_keeps_real_outcome(tmp_path):
    config = EngineConfig(mode="test", enforce_codes=frozenset({"RUNTIME_STATE_FORBIDS"}))
    q = TaskQueue(str(tmp_path / "tasks.db"), cea_config=config, cea_providers=dict(WIRED))
    q.enqueue(task(task_type="review", context=admitted()))
    q.dequeue(agent="codex", role="reviewer")
    nonce = q.record_dispatch("t1", channel="tmux_pane", agent="codex", target="%1")
    answer = q.start_execution("t1", nonce, presenter="gemini")
    assert answer["go"] is True
    assert answer["outcome"] == "BLOCK"
    assert answer["advisory"] is True and answer["review_required"] is True
    assert answer["enforced"] is False
    assert answer["instruction"] == "advisory verdict: not enforced — proceed; decide on go only"
