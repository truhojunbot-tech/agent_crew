"""Tests for the dispatcher's transient-error detector.

The dispatcher needs to distinguish *real* failures (agent crashed,
agent declined to respond) from *upstream throttle* (Anthropic 5h limiter
returning 429, Google MODEL_CAPACITY_EXHAUSTED on a preview model).
Real failures get marked failed; transient ones get requeued.
"""
from agent_crew.server import _detect_transient_error_in_log


def _write(tmp_path, content: str) -> str:
    p = tmp_path / "dispatch.log"
    p.write_text(content)
    return str(p)


def test_no_error_returns_none(tmp_path):
    log = _write(tmp_path, '{"type":"result","subtype":"success","is_error":false}\n')
    assert _detect_transient_error_in_log(log) is None


def test_claude_429_detected(tmp_path):
    log = _write(
        tmp_path,
        '{"type":"result","subtype":"success","is_error":true,'
        '"api_error_status":429,'
        '"result":"API Error: Server is temporarily limiting requests"}\n',
    )
    assert _detect_transient_error_in_log(log) == "claude_429"


def test_claude_throttle_text_detected(tmp_path):
    log = _write(tmp_path, "Server is temporarily limiting requests (not your usage limit) · Rate limited\n")
    assert _detect_transient_error_in_log(log) == "claude_throttle"


def test_gemini_capacity_exhausted_detected(tmp_path):
    log = _write(tmp_path, '"reason": "MODEL_CAPACITY_EXHAUSTED",\n')
    assert _detect_transient_error_in_log(log) == "gemini_capacity"


def test_gemini_resource_exhausted_detected(tmp_path):
    log = _write(tmp_path, '"status": "RESOURCE_EXHAUSTED",\n')
    assert _detect_transient_error_in_log(log) == "gemini_resource_exhausted"


def test_codex_capacity_detected(tmp_path):
    log = _write(tmp_path, "ERROR: Selected model is at capacity. Please try a different model.\n")
    assert _detect_transient_error_in_log(log) == "codex_capacity"


def test_agy_quota_exhausted_detected(tmp_path):
    log = _write(
        tmp_path,
        "Error: Individual quota reached. Please upgrade your subscription "
        "to increase your limits. Resets in 1h26m40s.\n",
    )
    assert _detect_transient_error_in_log(log) == "agy_quota_exhausted"


def test_agy_timeout_detected(tmp_path):
    log = _write(tmp_path, "I will run the entire test suite.\nError: timeout waiting for response\n")
    assert _detect_transient_error_in_log(log) == "agy_timeout"


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
    assert _detect_transient_error_in_log(log) == "agy_quota_exhausted"


def test_agy_subscriber_lag_detected_response_finished_variant(tmp_path):
    log = _write(
        tmp_path,
        "I will check the current system time.\n"
        "Error: the connection to the agent was interrupted before the "
        "response finished: subscriber fell behind updates, stalled for 6s\n",
    )
    assert _detect_transient_error_in_log(log) == "agy_subscriber_lag"


def test_agy_subscriber_lag_detected_response_started_variant(tmp_path):
    log = _write(
        tmp_path,
        "Error: the connection to the agent was interrupted before the "
        "response started: subscriber fell behind updates, stalled for 6s\n",
    )
    assert _detect_transient_error_in_log(log) == "agy_subscriber_lag"


def test_only_tail_is_scanned(tmp_path):
    # 20KB of innocuous prefix, transient marker only at the end.
    big = ("x" * 20480) + '"api_error_status":429'
    log = _write(tmp_path, big)
    assert _detect_transient_error_in_log(log, tail_bytes=4096) == "claude_429"


def test_missing_file_returns_none(tmp_path):
    assert _detect_transient_error_in_log(str(tmp_path / "nonexistent.log")) is None


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
    assert _detect_transient_error_in_log(log, since_offset=offset) == "agy_timeout"


def test_since_offset_still_detects_current_task_quota(tmp_path):
    marker = "=" * 60 + "\nTASK current-task | tester | 2026-07-28 10:45:39\n" + "=" * 60 + "\n"
    log = _write(tmp_path, "some earlier unrelated content\n")
    offset = len("some earlier unrelated content\n".encode("utf-8"))
    with open(log, "a") as f:
        f.write(marker)
        f.write("Error: Individual quota reached. Resets in 1h.\n")
    assert _detect_transient_error_in_log(log, since_offset=offset) == "agy_quota_exhausted"
