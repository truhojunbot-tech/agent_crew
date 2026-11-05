"""#993: agy's terminal "Individual quota reached" record is a provider limit."""

import json

import pytest

from agent_crew.failure_kind import classify_provider_record, provider_failure_from_log

_SAMPLES = [
    ("Individual quota reached. Please wait for your quota to reset. Resets in 112h9m25s.",
     112 * 3600 + 9 * 60 + 25),
    ("Individual quota reached. Please wait for your quota to reset. Resets in 117h56m32s.",
     117 * 3600 + 56 * 60 + 32),
]


@pytest.mark.parametrize("agent", ["agy", "gemini"])
@pytest.mark.parametrize("message,seconds", _SAMPLES)
def test_terminal_individual_quota_is_provider_limit_with_reset(agent, message, seconds):
    record = {"status": "ERROR", "error": message, "num_turns": 0}
    failure = classify_provider_record(agent, record)
    assert failure["kind"] == "provider_limit"
    assert failure["provider_code"] == "quota_exhausted"
    assert abs(failure["retry_after"] - seconds) <= 1


def test_partial_reset_durations_and_missing_reset():
    base = "Individual quota reached."
    assert classify_provider_record(
        "agy", {"status": "ERROR", "error": base + " Resets in 45m."})["retry_after"] == 2700
    assert classify_provider_record(
        "agy", {"status": "ERROR", "error": base + " Resets in 2h."})["retry_after"] == 7200
    failure = classify_provider_record("agy", {"status": "ERROR", "error": base})
    assert failure["kind"] == "provider_limit"
    assert failure["retry_after"] is None


def test_quota_phrase_must_anchor_the_terminal_error():
    failure = classify_provider_record(
        "agy", {"status": "ERROR", "error": "tool said: Individual quota reached. Resets in 1h."})
    assert failure["kind"] == "agent_failed"
    assert classify_provider_record(
        "agy", {"type": "message", "content": "Individual quota reached. Resets in 1h."}) is None


def test_transient_429_and_invalid_model_unchanged():
    transient = classify_provider_record("agy", {"status": "ERROR", "code": "RESOURCE_EXHAUSTED",
                                                 "error": "Individual quota reached."})
    assert transient == {"kind": "transient", "provider_code": "resource_exhausted",
                         "retry_after": None}
    assert classify_provider_record("gemini", {"status": "ERROR", "http_status": 429})["kind"] == "transient"
    invalid = classify_provider_record("agy", {"status": "ERROR", "num_turns": 0,
                                               "error": "invalid model selection"})
    assert invalid == {"kind": "config_error", "provider_code": "invalid_model",
                       "retry_after": None}


def test_log_end_to_end_reads_only_the_terminal_record(tmp_path):
    message, seconds = _SAMPLES[0]
    log = tmp_path / "dispatch_tester.log"
    log.write_text("\n".join([
        "error: Individual quota reached. Resets in 1h.",
        'AGY_ERROR: {"status":"RESOURCE_EXHAUSTED","error_code":429,'
        '"error":"Individual quota reached. Resets in 1h."}',
        json.dumps({"status": "ERROR", "error": message, "num_turns": 1}),
    ]) + "\n")
    failure = provider_failure_from_log(str(log), agent="agy")
    assert failure["kind"] == "provider_limit"
    assert failure["provider_code"] == "quota_exhausted"
    assert abs(failure["retry_after"] - seconds) <= 1

    prose_only = tmp_path / "prose.log"
    prose_only.write_text("\n".join([
        "error: Individual quota reached. Resets in 1h.",
        'AGY_ERROR: {"status":"ERROR","error":"Individual quota reached. Resets in 1h."}',
    ]) + "\n")
    assert provider_failure_from_log(str(prose_only), agent="agy") is None
