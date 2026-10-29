"""#190/#192: structured quota evidence takes precedence over task prose."""
import json

from agent_crew.failure_kind import provider_failure_from_log


def _log(tmp_path, record, prose=""):
    path = tmp_path / "dispatch.log"
    path.write_text(prose + "\n" + json.dumps(record) + "\n")
    return str(path)


def test_quota_exhausted_is_not_retriable(tmp_path):
    failure = provider_failure_from_log(
        _log(tmp_path, {"status": "ERROR", "reason": "QUOTA_EXHAUSTED"}),
        agent="gemini")
    assert failure["kind"] == "provider_limit"
    assert failure["retry_after"] is None


def test_model_capacity_has_cooldown(tmp_path):
    failure = provider_failure_from_log(
        _log(tmp_path, {"status": "ERROR", "reason": "MODEL_CAPACITY_EXHAUSTED"}),
        agent="gemini")
    assert failure["kind"] == "capacity"
    assert failure["retry_after"] == 120


def test_quota_prose_does_not_override_structured_timeout(tmp_path):
    failure = provider_failure_from_log(
        _log(tmp_path, {"status": "ERROR", "reason": "TIMEOUT"},
             prose="Quota exhausted in a sample prompt"), agent="gemini")
    assert failure["kind"] == "timeout"
