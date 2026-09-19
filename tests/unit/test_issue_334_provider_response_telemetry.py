"""#334 — terminal provider responses enrich the task-attribution row."""

import json

import pytest

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.telemetry_response import response_log_telemetry, response_telemetry


@pytest.mark.parametrize("provider,response", [
    ("claude", {"message": {"model": "claude-test", "usage": {
        "input_tokens": 11, "cache_creation_input_tokens": 12,
        "cache_read_input_tokens": 13, "output_tokens": 14, "reasoning_tokens": 15,
    }}}),
    ("codex", {"type": "turn.completed", "usage": {
        "input_tokens": 31, "cached_input_tokens": 9, "cache_write_input_tokens": 0,
        "output_tokens": 34, "reasoning_output_tokens": 35,
    }}),
    ("gemini", {"response": {"modelVersion": "gemini-test", "usageMetadata": {
        "promptTokenCount": 51, "cachedContentTokenCount": 3, "cacheCreationTokenCount": 52,
        "candidatesTokenCount": 54, "thoughtsTokenCount": 55,
    }}}),
])
def test_provider_response_usage_populates_all_observed_usage_components(tmp_path, provider, response):
    telemetry = response_telemetry(provider, response)
    for field in ("uncached_input_tokens", "cache_write_tokens", "cache_read_tokens",
                  "output_tokens", "reasoning_tokens", "context_window_tokens"):
        assert getattr(telemetry, field) is not None

    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(task_id=f"organic-{provider}", task_type="implement", description="d"))
    queue.record_attribution(task_id=f"organic-{provider}", agent=provider, status="in_progress")
    queue.record_task_telemetry(f"organic-{provider}", telemetry)
    row = queue.get_attribution(f"organic-{provider}")
    for field in ("uncached_input_tokens", "cache_write_tokens", "cache_read_tokens",
                  "output_tokens", "reasoning_tokens", "context_window_tokens"):
        assert row[field] is not None


def test_response_telemetry_preserves_absent_components_as_unknown():
    telemetry = response_telemetry("codex", {"usage": {"output_tokens": 7}})
    assert telemetry.output_tokens == 7
    assert telemetry.uncached_input_tokens is None
    assert telemetry.cache_read_tokens is None
    assert telemetry.cache_write_tokens is None


def test_claude_log_suffix_deduplicates_repeated_message_usage():
    record = {"session_id": "session-334", "message": {"id": "message-334", "usage": {
        "input_tokens": 2, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 5,
        "output_tokens": 7, "reasoning_tokens": 11,
    }}}
    telemetry = response_log_telemetry("claude", "\n".join((json.dumps(record), json.dumps(record))))
    assert (telemetry.uncached_input_tokens, telemetry.cache_write_tokens,
            telemetry.cache_read_tokens, telemetry.output_tokens,
            telemetry.reasoning_tokens, telemetry.context_window_tokens) == (2, 3, 5, 7, 11, 10)


def test_claude_terminal_result_usage_overrides_message_output_only():
    message = {"type": "assistant", "message": {"id": "message-334", "usage": {
        "input_tokens": 2, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 5,
        "output_tokens": 73,
    }}}
    result = {"type": "result", "usage": {"output_tokens": 11657}}

    telemetry = response_log_telemetry("claude", "\n".join((json.dumps(message), json.dumps(result))))

    assert telemetry.output_tokens == 11657
    assert (telemetry.uncached_input_tokens, telemetry.cache_write_tokens,
            telemetry.cache_read_tokens, telemetry.context_window_tokens) == (2, 3, 5, 10)
