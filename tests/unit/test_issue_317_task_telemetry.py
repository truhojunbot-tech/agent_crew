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


def _usage_record(invocation_id, usage, *, model="claude-test"):
    """Claude replays have distinct line UUIDs but retain message.id."""
    return json.dumps({
        "uuid": f"line-{invocation_id}",
        "message": {"id": invocation_id, "model": model, "usage": usage},
    }) + "\n"


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


def test_claude_adapter_sums_and_deduplicates_usage_in_a_task_span(tmp_path):
    cwd = "/worktrees/claude-317-span"
    path = _claude_session(tmp_path, cwd, {"input_tokens": 100})
    start = path.stat().st_size
    first = {"input_tokens": 2, "cache_creation_input_tokens": 3,
             "cache_read_input_tokens": 5, "output_tokens": 7,
             "reasoning_tokens": 11}
    second = {"input_tokens": 13, "cache_creation_input_tokens": 17,
              "cache_read_input_tokens": 19, "output_tokens": 23,
              "reasoning_tokens": 29}
    with path.open("a") as transcript:
        transcript.write(_usage_record("invocation-1", first))
        transcript.write(_usage_record("invocation-1", first))
        transcript.write(_usage_record("invocation-2", second))

    observed = ClaudeSessionTelemetryAdapter(home=tmp_path).extract(
        provider="claude", worktree_path=cwd,
        provider_session_id=_span_session_id(path.stem, start),
    )

    assert (observed.uncached_input_tokens, observed.cache_write_tokens,
            observed.cache_read_tokens, observed.output_tokens,
            observed.reasoning_tokens, observed.context_window_tokens) == (15, 20, 24, 30, 40, 59)


def _span_session_id(session_id, offset):
    """Queue uses this internal carrier without changing adapter's API."""
    from agent_crew.queue import _TaskProviderSessionId
    return _TaskProviderSessionId(session_id, {"session_id": session_id, "offset": offset})


def _fresh_session_id(snapshot):
    """Queue carrier for a fresh-task transcript directory snapshot."""
    from agent_crew.queue import _TaskProviderSessionId
    return _TaskProviderSessionId("", {"session_id": "", "offset": 0,
                                       "fresh_session_paths": snapshot})


def test_fresh_dispatch_boundary_snapshots_existing_transcript_paths(tmp_path, monkeypatch):
    from agent_crew import server

    cwd = "/worktrees/claude-321-dispatch"
    existing = _claude_session(tmp_path, cwd, {"input_tokens": 1})
    monkeypatch.setattr(server, "_claude_home", lambda home=None: tmp_path)

    assert server.claude_task_start_boundary(cwd) == {
        "session_id": "", "offset": 0, "fresh_session_paths": [str(existing)]}


def test_claude_adapter_reads_the_single_transcript_created_after_fresh_dispatch(tmp_path):
    cwd = "/worktrees/claude-321-fresh"
    directory = tmp_path / "projects" / re.sub(r"[/._]", "-", cwd)
    directory.mkdir(parents=True)
    created = directory / "created-by-task.jsonl"
    created.write_text(
        _usage_record("fresh-1", {"input_tokens": 2, "output_tokens": 3})
        + _usage_record("fresh-2", {"input_tokens": 5, "output_tokens": 7})
    )

    observed = ClaudeSessionTelemetryAdapter(home=tmp_path).extract(
        provider="claude", worktree_path=cwd, provider_session_id=_fresh_session_id([]))

    assert (observed.uncached_input_tokens, observed.output_tokens,
            observed.provider_session_id) == (7, 10, "created-by-task")


def test_claude_adapter_keeps_ambiguous_fresh_transcripts_unknown(tmp_path):
    cwd = "/worktrees/claude-321-ambiguous"
    old = _claude_session(tmp_path, cwd, {"input_tokens": 999})
    directory = old.parent
    (directory / "new-one.jsonl").write_text(_usage_record("one", {"input_tokens": 2}))
    (directory / "new-two.jsonl").write_text(_usage_record("two", {"input_tokens": 3}))

    observed = ClaudeSessionTelemetryAdapter(home=tmp_path).extract(
        provider="claude", worktree_path=cwd,
        provider_session_id=_fresh_session_id([str(old)]))

    assert observed.uncached_input_tokens is None
    assert observed.provider_session_id is None


def test_fresh_session_binding_survives_restart_and_next_task_uses_its_own_span(tmp_path):
    db = tmp_path / "tasks.db"
    cwd = "/worktrees/claude-321-continuity"
    directory = tmp_path / "projects" / re.sub(r"[/._]", "-", cwd)
    directory.mkdir(parents=True)
    session = directory / "fresh-session.jsonl"
    queue = TaskQueue(str(db), telemetry_adapter=ClaudeSessionTelemetryAdapter(home=tmp_path))
    context = queue.get_or_create_context("project", "claude", cwd, role="implementer",
                                          task_id="fresh-task")
    queue.enqueue(TaskRequest(task_id="fresh-task", task_type="implement", description="d"))
    queue.record_attribution(task_id="fresh-task", agent="claude", worktree_path=cwd,
                             context_id=context["context_id"], status="in_progress")
    queue.patch_context("fresh-task", {"claude_transcript_start": {
        "session_id": "", "offset": 0, "fresh_session_paths": []}})
    session.write_text(_usage_record("fresh", {"input_tokens": 11, "output_tokens": 13}))

    restarted = TaskQueue(str(db), telemetry_adapter=ClaudeSessionTelemetryAdapter(home=tmp_path))
    restarted.submit_result("fresh-task", TaskResult(
        task_id="fresh-task", status="completed", summary="done"))
    first = restarted.get_attribution("fresh-task")
    assert (first["uncached_input_tokens"], first["output_tokens"],
            first["provider_session_id"]) == (11, 13, "fresh-session")
    assert restarted.peek_context_provider_session_id("project", "claude", cwd) == "fresh-session"

    next_context = restarted.get_or_create_context("project", "claude", cwd,
                                                   role="implementer", task_id="resume-task")
    assert next_context["provider_session_id"] == "fresh-session"
    start = session.stat().st_size
    restarted.enqueue(TaskRequest(task_id="resume-task", task_type="implement", description="d"))
    restarted.record_attribution(task_id="resume-task", agent="claude", worktree_path=cwd,
                                 provider_session_id="fresh-session",
                                 context_id=next_context["context_id"], status="in_progress")
    restarted.patch_context("resume-task", {"claude_transcript_start": {
        "session_id": "fresh-session", "offset": start}})
    with session.open("a") as transcript:
        transcript.write(_usage_record("resume", {"input_tokens": 17, "output_tokens": 19}))
    restarted.submit_result("resume-task", TaskResult(
        task_id="resume-task", status="completed", summary="done"))
    second = restarted.get_attribution("resume-task")
    assert (second["uncached_input_tokens"], second["output_tokens"]) == (17, 19)


def test_sequential_tasks_use_only_their_own_transcript_spans(tmp_path):
    db = tmp_path / "tasks.db"
    cwd = "/worktrees/claude-317-sequential"
    path = _claude_session(tmp_path, cwd, {"input_tokens": 999})
    queue = TaskQueue(str(db), telemetry_adapter=ClaudeSessionTelemetryAdapter(home=tmp_path))
    for task_id in ("task-a", "task-b"):
        queue.enqueue(TaskRequest(task_id=task_id, task_type="implement", description="d"))
        queue.record_attribution(task_id=task_id, agent="claude", worktree_path=cwd,
                                 provider_session_id=path.stem, status="in_progress")

    a_start = path.stat().st_size
    with path.open("a") as transcript:
        transcript.write(_usage_record("a-1", {"input_tokens": 2, "output_tokens": 3}))
        transcript.write(_usage_record("a-2", {"input_tokens": 5, "output_tokens": 7}))
    queue.patch_context("task-a", {"claude_transcript_start": {
        "session_id": path.stem, "offset": a_start}})
    queue.submit_result("task-a", TaskResult(task_id="task-a", status="completed", summary="a"))

    b_start = path.stat().st_size
    with path.open("a") as transcript:
        transcript.write(_usage_record("b-1", {"input_tokens": 11, "output_tokens": 13}))
    queue.patch_context("task-b", {"claude_transcript_start": {
        "session_id": path.stem, "offset": b_start}})
    queue.submit_result("task-b", TaskResult(task_id="task-b", status="completed", summary="b"))

    a, b = queue.get_attribution("task-a"), queue.get_attribution("task-b")
    assert (a["uncached_input_tokens"], a["output_tokens"]) == (7, 10)
    assert (b["uncached_input_tokens"], b["output_tokens"]) == (11, 13)


def test_task_span_boundary_survives_queue_restart(tmp_path):
    db = tmp_path / "tasks.db"
    cwd = "/worktrees/claude-317-restart"
    path = _claude_session(tmp_path, cwd, {"input_tokens": 999})
    queue = TaskQueue(str(db), telemetry_adapter=ClaudeSessionTelemetryAdapter(home=tmp_path))
    queue.enqueue(TaskRequest(task_id="restart-span", task_type="implement", description="d"))
    queue.record_attribution(task_id="restart-span", agent="claude", worktree_path=cwd,
                             provider_session_id=path.stem, status="in_progress")
    start = path.stat().st_size
    queue.patch_context("restart-span", {"claude_transcript_start": {
        "session_id": path.stem, "offset": start}})
    with path.open("a") as transcript:
        transcript.write(_usage_record("restart-1", {"input_tokens": 31, "output_tokens": 37}))

    restarted = TaskQueue(str(db), telemetry_adapter=ClaudeSessionTelemetryAdapter(home=tmp_path))
    restarted.submit_result("restart-span", TaskResult(
        task_id="restart-span", status="completed", summary="done"))

    row = restarted.get_attribution("restart-span")
    assert (row["uncached_input_tokens"], row["output_tokens"]) == (31, 37)


def test_claude_dispatch_persists_the_existing_transcript_byte_boundary(tmp_path, monkeypatch):
    import asyncio
    import subprocess

    from fastapi.testclient import TestClient

    from agent_crew import server
    from agent_crew.server import create_app

    wt = tmp_path / "claude"
    wt.mkdir()
    (wt / ".git").mkdir()
    path = _claude_session(tmp_path, str(wt), {"input_tokens": 101})
    expected_offset = path.stat().st_size
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"worktrees": {"claude": str(wt)}}))
    db = str(tmp_path / "tasks.db")

    async def fake_exec(*args, **kwargs):
        class Process:
            returncode = 0
            pid = 1

            async def wait(self):
                return 0

        return Process()

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setattr(server, "_claude_home", lambda home=None: tmp_path)
    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, "", ""))

    app = create_app(db_path=db, pane_map={}, port=0, state_path=str(state), project="project",
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        queue = TaskQueue(db)
        context = queue.get_or_create_context("project", "claude", str(wt),
                                              role="implementer", task_id="prior")
        queue.update_context_provider_session_id(context["context_key"], path.stem)
        queue.enqueue(TaskRequest(task_id="span-dispatch", task_type="implement",
                                  description="d", project="project"))
        task = queue.dequeue(role="implementer")
        assert task is not None
        asyncio.run(app.state.dispatch_task(task, "implementer"))

    persisted = TaskQueue(db).get_task_context("span-dispatch")
    assert persisted["claude_transcript_start"] == {
        "session_id": path.stem, "offset": expected_offset}


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
    queue.patch_context("telemetry-317", {"context_pack_hash": "pack-317-hash"})

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
    assert row["context_pack_hash"] == "pack-317-hash"
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
