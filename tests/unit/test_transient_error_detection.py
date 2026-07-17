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


def test_only_tail_is_scanned(tmp_path):
    # 20KB of innocuous prefix, transient marker only at the end.
    big = ("x" * 20480) + '"api_error_status":429'
    log = _write(tmp_path, big)
    assert _detect_transient_error_in_log(log, tail_bytes=4096) == "claude_429"


def test_missing_file_returns_none(tmp_path):
    assert _detect_transient_error_in_log(str(tmp_path / "nonexistent.log")) is None
