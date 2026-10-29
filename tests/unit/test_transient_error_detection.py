"""Provider JSON events produce typed failures; diagnostic prose has no authority."""
import json

import pytest

from agent_crew.failure_kind import provider_failure_from_log


def _log(tmp_path, *lines):
    path = tmp_path / "dispatch.log"
    path.write_text("\n".join(lines) + "\n")
    return str(path)


@pytest.mark.parametrize("agent,event,kind,code", [
    ("claude", {"type": "result", "is_error": True, "api_error_status": 429},
     "transient", "http_429"),
    ("gemini", {"status": "ERROR", "reason": "MODEL_CAPACITY_EXHAUSTED"},
     "capacity", "model_capacity_exhausted"),
    ("gemini", {"status": "ERROR", "reason": "QUOTA_EXHAUSTED"},
     "provider_limit", "quota_exhausted"),
    ("codex", {"type": "error", "code": "CODEX_CAPACITY"},
     "capacity", "codex_capacity"),
    ("gemini", {"status": "ERROR", "reason": "TIMEOUT"},
     "timeout", "timeout"),
])
def test_structured_provider_failure(tmp_path, agent, event, kind, code):
    failure = provider_failure_from_log(
        _log(tmp_path, json.dumps(event)), agent=agent)
    assert failure["kind"] == kind
    assert failure["provider_code"] == code


def test_agent_prose_and_echoed_commands_do_not_classify(tmp_path):
    text = "quota exceeded; Selected model is at capacity; api_error_status:429"
    echo = json.dumps({"type": "item.completed", "item": {
        "type": "command_execution", "aggregated_output": text}})
    for agent in ("claude", "codex", "gemini"):
        assert provider_failure_from_log(_log(tmp_path, text, echo), agent=agent) is None


def test_provider_event_is_scoped_to_own_adapter(tmp_path):
    event = json.dumps({"type": "result", "is_error": True,
                        "api_error_status": 429})
    path = _log(tmp_path, event)
    assert provider_failure_from_log(path, agent="claude") is not None
    assert provider_failure_from_log(path, agent="codex") is None


def test_previous_attempt_is_not_read(tmp_path):
    prior = json.dumps({"type": "error", "code": "CODEX_CAPACITY"}) + "\n"
    path = _log(tmp_path, prior, "new task succeeded")
    assert provider_failure_from_log(path, agent="codex",
                                          since_offset=len(prior.encode()) + 1) is None


def test_missing_log_is_unknown(tmp_path):
    assert provider_failure_from_log(str(tmp_path / "missing.log"), agent="codex") is None
