"""#317 result transports persist real provider telemetry through TaskQueue."""

import asyncio
import json
import re

from agent_crew.mcp_server import build_mcp_server
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.telemetry import ClaudeSessionTelemetryAdapter


def _session(home, cwd):
    directory = home / "projects" / re.sub(r"[/._]", "-", cwd)
    directory.mkdir(parents=True)
    (directory / "session-317.jsonl").write_text(json.dumps({"message": {"usage": {
        "input_tokens": 2, "cache_creation_input_tokens": 3,
        "cache_read_input_tokens": 5, "output_tokens": 7,
    }}}) + "\n")


def _call(mcp, name, **kwargs):
    fn = mcp._tool_manager._tools[name].fn
    return asyncio.run(fn(**kwargs)) if asyncio.iscoroutinefunction(fn) else fn(**kwargs)


def _seed(db, task_id, cwd):
    queue = TaskQueue(str(db))
    queue.enqueue(TaskRequest(task_id=task_id, task_type="implement", description="d"))
    queue.record_attribution(task_id=task_id, agent="claude", worktree_path=cwd,
                             provider_session_id="session-317", status="in_progress")


def test_mcp_result_submission_captures_claude_usage(tmp_path, monkeypatch):
    import agent_crew.queue as queue_module

    cwd = "/worktrees/transport-317"
    _session(tmp_path, cwd)
    monkeypatch.setattr(queue_module, "default_telemetry_adapter",
                        lambda: ClaudeSessionTelemetryAdapter(home=tmp_path))

    mcp_db = tmp_path / "mcp.db"
    mcp = build_mcp_server(str(mcp_db))
    _seed(mcp_db, "mcp-317", cwd)
    response = _call(mcp, "submit_result", task_id="mcp-317", status="completed", summary="done")
    assert response["acknowledged"] is True

    row = TaskQueue(str(mcp_db)).get_attribution("mcp-317")
    assert (row["uncached_input_tokens"], row["cache_write_tokens"],
            row["cache_read_tokens"], row["output_tokens"],
            row["context_window_tokens"]) == (2, 3, 5, 7, 10)
