"""#334 — terminal provider responses enrich the task-attribution row."""

import json

import pytest

from agent_crew.protocol import TaskRequest
from agent_crew.protocol import TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.telemetry import TaskTelemetry
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
    result = {"type": "result", "usage": {"output_tokens": 7}}
    telemetry = response_log_telemetry("claude", "\n".join((json.dumps(record), json.dumps(record), json.dumps(result))))
    assert (telemetry.uncached_input_tokens, telemetry.cache_write_tokens,
            telemetry.cache_read_tokens, telemetry.output_tokens,
            telemetry.reasoning_tokens, telemetry.context_window_tokens) == (2, 3, 5, 7, 11, 10)


def test_claude_context_window_is_peak_request_not_task_span_sum():
    first = {"type": "assistant", "message": {"id": "one", "usage": {
        "input_tokens": 2, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 5,
    }}}
    second = {"type": "assistant", "message": {"id": "two", "usage": {
        "input_tokens": 11, "cache_creation_input_tokens": 13, "cache_read_input_tokens": 17,
    }}}
    result = {"type": "result", "usage": {"output_tokens": 1}}

    telemetry = response_log_telemetry("claude", "\n".join(map(json.dumps, (first, second, result))))

    assert telemetry.context_window_tokens == 41
    assert (telemetry.uncached_input_tokens, telemetry.cache_write_tokens,
            telemetry.cache_read_tokens) == (13, 16, 22)


def test_claude_without_terminal_result_keeps_output_unknown():
    message = {"type": "assistant", "message": {"id": "message-334", "usage": {
        "input_tokens": 2, "output_tokens": 73,
    }}}

    telemetry = response_log_telemetry("claude", json.dumps(message))

    assert telemetry.output_tokens is None


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


def _codex_task(queue, task_id, session, index, **usage):
    queue.enqueue(TaskRequest(task_id=task_id, task_type="implement", description="d"))
    queue.record_attribution(task_id, agent="codex", provider_session_id=session,
                             session_task_index=index, status="in_progress")
    queue.record_task_telemetry(task_id, TaskTelemetry(**usage))
    row = queue.get_attribution(task_id)
    return row, json.loads(queue.get_tokenomics_shadow_receipt(task_id)["economics_json"])


def test_codex_resumed_thread_receipt_uses_per_task_delta_and_keeps_cumulative(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    first, _ = _codex_task(queue, "first", "thread-a", 1,
                           uncached_input_tokens=100, cache_read_tokens=200,
                           output_tokens=30, reasoning_tokens=10, context_window_tokens=300)
    second, economics = _codex_task(queue, "second", "thread-a", 2,
                                    uncached_input_tokens=115, cache_read_tokens=250,
                                    output_tokens=38, reasoning_tokens=13,
                                    context_window_tokens=365)
    assert first["output_tokens"] == 30
    for field, expected in {"uncached_input_tokens": 15, "cache_read_tokens": 50,
                            "output_tokens": 8, "reasoning_tokens": 3,
                            "context_window_tokens": 65}.items():
        assert second[field] == economics[field] == expected
    cumulative = {"uncached_input_tokens": 115, "cache_read_tokens": 250,
                  "cache_write_tokens": None,
                  "output_tokens": 38, "reasoning_tokens": 13,
                  "context_window_tokens": 365}
    assert json.loads(second["codex_thread_cumulative"]) == cumulative
    assert economics["codex_thread_cumulative"] == cumulative


def test_codex_first_task_and_changed_session_do_not_subtract(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    first, _ = _codex_task(queue, "first", "thread-a", 1, output_tokens=30)
    # A fresh provider session starts a new context generation and index.
    renewed, economics = _codex_task(queue, "renewed", "thread-b", 1, output_tokens=7)
    assert first["output_tokens"] == 30
    assert renewed["output_tokens"] == economics["output_tokens"] == 7


def test_codex_resumed_task_with_missing_earlier_attribution_has_unknown_delta(tmp_path, caplog):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    row, economics = _codex_task(queue, "second", "thread-a", 2,
                                 uncached_input_tokens=100, output_tokens=20)
    assert row["uncached_input_tokens"] is economics["uncached_input_tokens"] is None
    assert row["output_tokens"] is economics["output_tokens"] is None
    assert json.loads(row["codex_thread_cumulative"])["output_tokens"] == 20
    assert "no earlier attribution" in caplog.text


def test_codex_negative_or_unknown_previous_field_is_null(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _codex_task(queue, "first", "thread-a", 1, output_tokens=30)
    second, economics = _codex_task(queue, "second", "thread-a", 2,
                                    output_tokens=20, reasoning_tokens=4)
    assert second["output_tokens"] is economics["output_tokens"] is None
    assert second["reasoning_tokens"] is economics["reasoning_tokens"] is None
    assert json.loads(second["codex_thread_cumulative"])["output_tokens"] == 20


def test_codex_previous_task_without_usage_keeps_delta_unknown(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(task_id="first", task_type="implement", description="d"))
    queue.record_attribution("first", agent="codex", provider_session_id="thread-a",
                             session_task_index=1)
    second, _ = _codex_task(queue, "second", "thread-a", 2, output_tokens=20)
    assert second["output_tokens"] is None


@pytest.mark.parametrize("provider", ["claude", "gemini"])
def test_other_provider_receipts_do_not_use_codex_delta(tmp_path, provider):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _codex_task(queue, "first", "shared-id", 1, output_tokens=30)
    queue.enqueue(TaskRequest(task_id="other", task_type="implement", description="d"))
    queue.record_attribution("other", agent=provider, provider_session_id="shared-id",
                             session_task_index=2)
    queue.record_task_telemetry("other", TaskTelemetry(output_tokens=7))
    row = queue.get_attribution("other")
    economics = json.loads(queue.get_tokenomics_shadow_receipt("other")["economics_json"])
    assert row["output_tokens"] == economics["output_tokens"] == 7
    assert row["codex_thread_cumulative"] is None
    assert "codex_thread_cumulative" not in economics


def test_codex_log_uses_latest_cumulative_turn_not_sum():
    records = [
        {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 60,
                                              "output_tokens": 10}},
        {"type": "turn.completed", "usage": {"input_tokens": 140, "cached_input_tokens": 80,
                                              "output_tokens": 15}},
    ]
    telemetry = response_log_telemetry("codex", "\n".join(map(json.dumps, records)))
    assert (telemetry.uncached_input_tokens, telemetry.cache_read_tokens,
            telemetry.output_tokens) == (60, 80, 15)


def test_codex_result_before_terminal_usage_is_filled_at_exit(tmp_path):
    from agent_crew.server import _record_exit_response_telemetry

    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _codex_task(queue, "first", "thread-a", 1,
                uncached_input_tokens=40, cache_read_tokens=60,
                output_tokens=10, context_window_tokens=100)
    queue.enqueue(TaskRequest(task_id="second", task_type="implement", description="d"))
    queue.record_attribution("second", agent="codex", provider_session_id="thread-a",
                             session_task_index=2)
    queue.submit_result("second", TaskResult(task_id="second", status="completed", summary="done"))
    assert queue.get_attribution("second")["codex_thread_cumulative"] is None

    tail = json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 140, "cached_input_tokens": 80, "output_tokens": 15,
    }})
    _record_exit_response_telemetry(queue, "second", "codex", tail)
    row = queue.get_attribution("second")
    economics = json.loads(queue.get_tokenomics_shadow_receipt("second")["economics_json"])
    assert (row["uncached_input_tokens"], row["cache_read_tokens"],
            row["output_tokens"]) == (20, 20, 5)
    assert economics["output_tokens"] == 5
    assert economics["codex_thread_cumulative"]["output_tokens"] == 15


def test_codex_exit_without_terminal_usage_keeps_receipt_unknown(tmp_path):
    from agent_crew.server import _record_exit_response_telemetry

    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(task_id="only", task_type="implement", description="d"))
    queue.record_attribution("only", agent="codex", provider_session_id="thread-a",
                             session_task_index=1)
    queue.submit_result("only", TaskResult(task_id="only", status="completed", summary="done"))
    _record_exit_response_telemetry(queue, "only", "codex", '{"type":"turn.started"}')
    assert queue.get_attribution("only")["output_tokens"] is None
    assert queue.get_attribution("only")["codex_thread_cumulative"] is None


def test_codex_exit_does_not_overwrite_existing_receipt(tmp_path):
    from agent_crew.server import _record_exit_response_telemetry

    queue = TaskQueue(str(tmp_path / "tasks.db"))
    before, _ = _codex_task(queue, "only", "thread-a", 1, output_tokens=10)
    queue.submit_result("only", TaskResult(task_id="only", status="completed", summary="done"))
    assert queue.get_attribution("only")["output_tokens"] == 10
    _record_exit_response_telemetry(queue, "only", "codex", json.dumps({
        "type": "turn.completed", "usage": {
            "input_tokens": 20, "cached_input_tokens": 5, "output_tokens": 999,
        },
    }))
    after = queue.get_attribution("only")
    assert after["output_tokens"] == before["output_tokens"] == 10
    assert after["uncached_input_tokens"] == 15
    cumulative = json.loads(after["codex_thread_cumulative"])
    assert cumulative["output_tokens"] == 10
    assert cumulative["uncached_input_tokens"] == 15
    economics = json.loads(queue.get_tokenomics_shadow_receipt("only")["economics_json"])
    assert economics["output_tokens"] == 10
    assert economics["codex_thread_cumulative"] == cumulative


@pytest.mark.parametrize("provider,tail", [
    ("claude", json.dumps({"type": "result", "usage": {"output_tokens": 7}})),
    ("gemini", json.dumps({"response": {"usageMetadata": {"candidatesTokenCount": 7}}})),
])
def test_other_providers_keep_exit_telemetry_path(tmp_path, provider, tail):
    from agent_crew.server import _record_exit_response_telemetry

    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(task_id="only", task_type="implement", description="d"))
    queue.record_attribution("only", agent=provider)
    _record_exit_response_telemetry(queue, "only", provider, tail)
    assert queue.get_attribution("only")["output_tokens"] == 7


def test_dispatch_exit_refreshes_usage_after_result_submission(tmp_path, monkeypatch, unused_tcp_port):
    import asyncio

    from fastapi.testclient import TestClient
    from agent_crew import server
    from agent_crew.server import create_app

    wt = tmp_path / "codex"
    wt.mkdir()
    (wt / ".git").mkdir()
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "role_agents": {"implementer": "codex", "reviewer": "claude", "tester": "gemini"},
        "worktrees": {"codex": str(wt)},
    }))
    db = str(tmp_path / "tasks.db")
    observations = []

    async def fake_exec(*cmd, **kwargs):
        path = kwargs["stdout"].name

        class Process:
            pid = 4242
            returncode = None

            async def wait(self):
                queue = TaskQueue(db)
                queue.submit_result("exit-usage", TaskResult(
                    task_id="exit-usage", status="completed", summary="done"))
                observations.append(queue.get_attribution("exit-usage")["codex_thread_cumulative"])
                with open(path, "a") as log:
                    log.write(json.dumps({"type": "turn.completed", "usage": {
                        "input_tokens": 20, "cached_input_tokens": 5, "output_tokens": 7,
                    }}) + "\n")
                self.returncode = 0
                return 0

        return Process()

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_exec)
    app = create_app(db_path=db, pane_map={}, port=unused_tcp_port,
                     state_path=str(state), project="p", watchdog_disabled=True,
                     anomaly_disabled=True, fallback_disabled=True)
    with TestClient(app):
        queue = TaskQueue(db)
        queue.enqueue(TaskRequest(task_id="exit-usage", task_type="implement",
                                  description="d", project="p"))
        task = queue.dequeue(role="implementer")
        assert task is not None
        asyncio.run(app.state.dispatch_task(task, "implementer"))
    assert observations == [None]
    row = TaskQueue(db).get_attribution("exit-usage")
    assert row["output_tokens"] == 7
    assert json.loads(row["codex_thread_cumulative"])["output_tokens"] == 7
