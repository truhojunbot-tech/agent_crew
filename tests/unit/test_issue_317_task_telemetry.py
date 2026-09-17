"""#317 — provider-neutral, task-level telemetry stays measured or NULL."""

import json
import re

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.telemetry import ClaudeSessionTelemetryAdapter


def _claude_session(home, cwd, usage, *, model="claude-test"):
    directory = home / "projects" / re.sub(r"[/._]", "-", cwd)
    directory.mkdir(parents=True)
    path = directory / "session-317.jsonl"
    path.write_text(json.dumps({"message": {"model": model, "usage": usage}}) + "\n")
    return path


def test_claude_adapter_reads_only_explicit_usage_fields(tmp_path):
    cwd = "/worktrees/claude-317"
    _claude_session(tmp_path, cwd, {
        "input_tokens": 12,
        "cache_creation_input_tokens": 34,
        "cache_read_input_tokens": 56,
        "output_tokens": 78,
        "reasoning_tokens": 9,
    })

    observed = ClaudeSessionTelemetryAdapter(home=tmp_path).extract(
        provider="claude", worktree_path=cwd, provider_session_id="session-317"
    )

    assert observed.uncached_input_tokens == 12
    assert observed.cache_write_tokens == 34
    assert observed.cache_read_tokens == 56
    assert observed.output_tokens == 78
    assert observed.reasoning_tokens == 9
    assert observed.context_window_tokens == 102
    assert observed.model == "claude-test"
    assert observed.provider_session_id == "session-317"
    assert observed.stable_prefix_hash is None
    assert observed.context_pack_hash is None


def test_claude_adapter_keeps_missing_usage_unknown(tmp_path):
    cwd = "/worktrees/claude-317-unknown"
    _claude_session(tmp_path, cwd, {"output_tokens": 7})

    observed = ClaudeSessionTelemetryAdapter(home=tmp_path).extract(
        provider="claude", worktree_path=cwd, provider_session_id="session-317"
    )

    assert observed.uncached_input_tokens is None
    assert observed.cache_write_tokens is None
    assert observed.cache_read_tokens is None
    assert observed.context_window_tokens is None
    assert observed.reasoning_tokens is None
    assert observed.output_tokens == 7


def test_result_submission_persists_adapter_telemetry_and_lifecycle(tmp_path):
    db = tmp_path / "tasks.db"
    cwd = "/worktrees/claude-317-result"
    _claude_session(tmp_path, cwd, {
        "input_tokens": 3, "cache_creation_input_tokens": 5,
        "cache_read_input_tokens": 7, "output_tokens": 11,
    })
    queue = TaskQueue(str(db), telemetry_adapter=ClaudeSessionTelemetryAdapter(home=tmp_path))
    queue.enqueue(TaskRequest(task_id="telemetry-317", task_type="implement", description="d"))
    queue.record_attribution(
        task_id="telemetry-317", agent="claude", model="dispatch-model",
        provider_session_id="session-317", worktree_path=cwd, status="in_progress",
        retry_of="earlier-attempt", fallback_of="fallback-source",
    )

    queue.submit_result("telemetry-317", TaskResult(
        task_id="telemetry-317", status="failed", summary="failed",
        error_info={"reason": "provider_error"},
    ))
    restarted = TaskQueue(str(db), telemetry_adapter=ClaudeSessionTelemetryAdapter(home=tmp_path))
    row = restarted.get_attribution("telemetry-317")

    assert row["agent"] == "claude"
    assert row["model"] == "dispatch-model", "dispatch attribution wins over transcript metadata"
    assert row["provider_session_id"] == "session-317"
    assert row["uncached_input_tokens"] == 3
    assert row["cache_write_tokens"] == 5
    assert row["cache_read_tokens"] == 7
    assert row["output_tokens"] == 11
    assert row["reasoning_tokens"] is None
    assert row["context_window_tokens"] == 15
    assert row["stable_prefix_hash"] is None
    assert row["context_pack_hash"] is None
    assert row["status"] == "failed"
    assert row["outcome"] == "failed:provider_error"
    assert row["retry_of"] == "earlier-attempt"
    assert row["fallback_of"] == "fallback-source"


def test_result_submission_fills_absent_model_and_session_from_transcript(tmp_path):
    db = tmp_path / "tasks.db"
    cwd = "/worktrees/claude-317-metadata"
    _claude_session(tmp_path, cwd, {"input_tokens": 1})
    queue = TaskQueue(str(db), telemetry_adapter=ClaudeSessionTelemetryAdapter(home=tmp_path))
    queue.enqueue(TaskRequest(task_id="metadata-317", task_type="implement", description="d"))
    queue.record_attribution(task_id="metadata-317", agent="claude", worktree_path=cwd,
                             status="in_progress")

    queue.submit_result("metadata-317", TaskResult(
        task_id="metadata-317", status="completed", summary="done"))
    row = queue.get_attribution("metadata-317")

    assert row["model"] == "claude-test"
    assert row["provider_session_id"] == "session-317"
