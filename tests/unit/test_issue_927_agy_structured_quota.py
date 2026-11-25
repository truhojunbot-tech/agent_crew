"""Structured AGY quota evidence keeps its retry time through log ingestion."""

import json

import pytest

from agent_crew.failure_kind import classify_provider_record, provider_failure_from_log


@pytest.mark.parametrize("agent", ["agy", "gemini"])
@pytest.mark.parametrize("timing,delay", [
    ({"retry_delay": 30}, 30),
    ({"retryDelay": "1.5s"}, 1.5),
    ({"retry_after": 0}, 0),
    ({"reset_at": 1060}, 60),
    ({"reset_at": "1970-01-01T00:17:40Z"}, 60),
    ({}, None),
    ({"retry_delay": -1}, None),
    ({"retry_delay": True}, None),
    ({"retry_delay": "NaNs"}, None),
])
def test_structured_quota_retry_time(agent, timing, delay):
    record = {"status": "RESOURCE_EXHAUSTED", "error_code": 429,
              "retryable": True, **timing}
    failure = classify_provider_record(agent, record, failed_at=1000)
    assert failure["kind"] == "provider_limit"
    assert failure["retry_after"] == delay
    assert failure.get("retry_at") == (1000 + delay if delay is not None else None)


@pytest.mark.parametrize("prefix", ["", "AGY_ERROR: "])
def test_nested_retry_info_survives_terminal_error(tmp_path, prefix):
    record = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "45s"}
    ]}}
    path = tmp_path / "provider.log"
    path.write_text(prefix + json.dumps(record) + '\n'
                    + json.dumps({"status": "ERROR", "error": "request failed"}) + '\n')
    failure = provider_failure_from_log(str(path), agent="gemini", failed_at=1000)
    assert failure == {"kind": "provider_limit", "provider_code": "resource_exhausted",
                       "retry_after": 45, "retry_at": 1045}


@pytest.mark.parametrize("record,kind", [
    ({"status": "ERROR", "http_status": 429}, "transient"),
    ({"status": "ERROR", "code": "RESOURCE_EXHAUSTED"}, "transient"),
    ({"status": "ERROR", "error": {"code": 429, "status": "OTHER"}}, "transient"),
    ({"status": "ERROR", "error": {"code": 500, "status": "RESOURCE_EXHAUSTED"}}, "agent_failed"),
])
def test_other_errors_unchanged(record, kind):
    assert classify_provider_record("agy", record)["kind"] == kind


def test_embedded_quota_json_is_not_provider_evidence(tmp_path):
    path = tmp_path / "provider.log"
    quota = {"status": "RESOURCE_EXHAUSTED", "error_code": 429}
    path.write_text("example AGY_ERROR: " + json.dumps(quota) + '\n'
                    + json.dumps({"type": "message", "content": quota}) + '\n')
    assert provider_failure_from_log(str(path), agent="gemini") is None
