"""Tests for the dispatcher's transient-error detector.

The dispatcher needs to distinguish *real* failures (agent crashed,
agent declined to respond) from *upstream throttle* (Anthropic 5h limiter
returning 429, Google MODEL_CAPACITY_EXHAUSTED on a preview model).
Real failures get marked failed; transient ones get requeued.
"""
from agent_crew.server import _detect_transient_error_in_log
import json

import pytest


def _write(tmp_path, content: str) -> str:
    p = tmp_path / "dispatch.log"
    p.write_text(content)
    return str(p)


def test_no_error_returns_none(tmp_path):
    log = _write(tmp_path, '{"type":"result","subtype":"success","is_error":false}\n')
    assert _detect_transient_error_in_log(log, agent="codex") is None


def test_claude_429_detected(tmp_path):
    log = _write(
        tmp_path,
        '{"type":"result","subtype":"success","is_error":true,'
        '"api_error_status":429,'
        '"result":"API Error: Server is temporarily limiting requests"}\n',
    )
    assert _detect_transient_error_in_log(log, agent="claude") == "claude_429"


def test_claude_throttle_text_detected(tmp_path):
    log = _write(tmp_path, "Server is temporarily limiting requests (not your usage limit) · Rate limited\n")
    assert _detect_transient_error_in_log(log, agent="claude") == "claude_throttle"


def test_gemini_capacity_exhausted_detected(tmp_path):
    log = _write(tmp_path, '"reason": "MODEL_CAPACITY_EXHAUSTED",\n')
    assert _detect_transient_error_in_log(log, agent="gemini") == "gemini_capacity"


def test_gemini_resource_exhausted_detected(tmp_path):
    log = _write(tmp_path, '"status": "RESOURCE_EXHAUSTED",\n')
    assert _detect_transient_error_in_log(log, agent="gemini") == "gemini_resource_exhausted"


def test_codex_capacity_detected(tmp_path):
    log = _write(tmp_path, "ERROR: Selected model is at capacity. Please try a different model.\n")
    assert _detect_transient_error_in_log(log, agent="codex") == "codex_capacity"


def test_agy_quota_exhausted_detected(tmp_path):
    log = _write(
        tmp_path,
        "Error: Individual quota reached. Please upgrade your subscription "
        "to increase your limits. Resets in 1h26m40s.\n",
    )
    assert _detect_transient_error_in_log(log, agent="gemini") == "agy_quota_exhausted"


def test_agy_timeout_detected(tmp_path):
    log = _write(tmp_path, "I will run the entire test suite.\nError: timeout waiting for response\n")
    assert _detect_transient_error_in_log(log, agent="gemini") == "agy_timeout"


def test_agy_quota_takes_priority_over_agy_timeout(tmp_path):
    # Both signatures could plausibly appear together; quota is the more
    # specific / actionable diagnosis (retry is definitely futile until
    # reset), so it must win over the generic timeout tag.
    log = _write(
        tmp_path,
        "Error: timeout waiting for response\n"
        "Error: Individual quota reached. Please upgrade your subscription "
        "to increase your limits. Resets in 5m.\n",
    )
    assert _detect_transient_error_in_log(log, agent="gemini") == "agy_quota_exhausted"


def test_agy_subscriber_lag_detected_response_finished_variant(tmp_path):
    log = _write(
        tmp_path,
        "I will check the current system time.\n"
        "Error: the connection to the agent was interrupted before the "
        "response finished: subscriber fell behind updates, stalled for 6s\n",
    )
    assert _detect_transient_error_in_log(log, agent="gemini") == "agy_subscriber_lag"


def test_agy_subscriber_lag_detected_response_started_variant(tmp_path):
    log = _write(
        tmp_path,
        "Error: the connection to the agent was interrupted before the "
        "response started: subscriber fell behind updates, stalled for 6s\n",
    )
    assert _detect_transient_error_in_log(log, agent="gemini") == "agy_subscriber_lag"


def test_only_tail_is_scanned(tmp_path):
    # 20KB of innocuous prefix, transient marker only at the end.
    big = ("x" * 20480) + '"api_error_status":429'
    log = _write(tmp_path, big)
    assert _detect_transient_error_in_log(log, tail_bytes=4096, agent="claude") == "claude_429"


def test_missing_file_returns_none(tmp_path):
    assert _detect_transient_error_in_log(str(tmp_path / "nonexistent.log"), agent="codex") is None


def test_since_offset_ignores_prior_task_error(tmp_path):
    # dispatch_{role}.log is shared across every task for that role. A
    # previous task's non-retryable quota message sitting just before EOF
    # must not bleed into detection for the *current* task, whose own
    # output (after since_offset) only contains a retryable timeout (#200).
    prior = "Error: Individual quota reached. Please upgrade your subscription.\n"
    marker = "=" * 60 + "\nTASK current-task | tester | 2026-07-28 10:45:39\n" + "=" * 60 + "\n"
    log = _write(tmp_path, prior)
    offset = len(prior.encode("utf-8"))
    with open(log, "a") as f:
        f.write(marker)
        f.write("Error: timeout waiting for response\n")
    assert _detect_transient_error_in_log(log, since_offset=offset, agent="gemini") == "agy_timeout"


def test_since_offset_still_detects_current_task_quota(tmp_path):
    marker = "=" * 60 + "\nTASK current-task | tester | 2026-07-28 10:45:39\n" + "=" * 60 + "\n"
    log = _write(tmp_path, "some earlier unrelated content\n")
    offset = len("some earlier unrelated content\n".encode("utf-8"))
    with open(log, "a") as f:
        f.write(marker)
        f.write("Error: Individual quota reached. Resets in 1h.\n")
    assert _detect_transient_error_in_log(log, since_offset=offset, agent="gemini") == "agy_quota_exhausted"


@pytest.mark.parametrize("marker", [
    "QUOTA_" + "EXHAUSTED",
    "Your quota will " + "reset",
    "Ineligible" + "TierError",
    "Individual quota" + " reached",
    '"api_error_' + 'status":429',
    "Server is temporarily limiting " + "requests",
    "MODEL_CAPACITY_" + "EXHAUSTED",
    "RESOURCE_" + "EXHAUSTED",
    "Selected model is at " + "capacity",
    "Error: timeout waiting for " + "response",
    "subscriber fell behind " + "updates",
])
def test_codex_command_output_does_not_classify_provider_markers(tmp_path, marker):
    event = {"type": "item.completed", "item": {"type": "command_execution",
             "aggregated_output": marker}}
    log = _write(tmp_path, json.dumps(event) + "\n")
    assert _detect_transient_error_in_log(log, agent="codex") is None


@pytest.mark.parametrize("item_type,field", [
    ("command_execution", "command"),
    ("agent_message", "text"),
])
def test_codex_non_error_events_do_not_classify_capacity(tmp_path, item_type, field):
    marker = "Selected model is at capacity"
    event = {"type": "item.completed", "item": {"type": item_type, field: marker}}
    log = _write(tmp_path, json.dumps(event) + "\n")
    assert _detect_transient_error_in_log(log, agent="codex") is None


def test_codex_partial_command_output_line_is_discarded(tmp_path):
    marker = "Selected model is at capacity"
    event = {"type": "item.completed", "item": {
        "type": "command_execution", "aggregated_output": "x" * 20000 + marker}}
    log = _write(tmp_path, json.dumps(event) + "\n")
    assert _detect_transient_error_in_log(log, tail_bytes=16384, agent="codex") is None


def test_codex_error_event_still_classifies_capacity(tmp_path):
    event = {"type": "error", "message": "Selected model is at capacity"}
    log = _write(tmp_path, json.dumps(event) + "\n")
    assert _detect_transient_error_in_log(log, agent="codex") == "codex_capacity"


def test_provider_markers_only_apply_to_their_provider(tmp_path):
    log = _write(tmp_path, "Ineligible" + "TierError")
    assert _detect_transient_error_in_log(log, agent="codex") is None
    assert _detect_transient_error_in_log(log, agent="claude") is None
    assert _detect_transient_error_in_log(log, agent="gemini") == "gemini_ineligible_tier"


@pytest.mark.parametrize("marker,owner,other,tag", [
    ('"api_error_' + 'status":429', "claude", "codex", "claude_429"),
    ("Selected model is at " + "capacity", "codex", "claude", "codex_capacity"),
    ("Individual quota" + " reached", "gemini", "claude", "agy_quota_exhausted"),
])
def test_plain_provider_diagnostics_remain_scoped(tmp_path, marker, owner, other, tag):
    log = _write(tmp_path, marker)
    assert _detect_transient_error_in_log(log, agent=owner) == tag
    assert _detect_transient_error_in_log(log, agent=other) is None
