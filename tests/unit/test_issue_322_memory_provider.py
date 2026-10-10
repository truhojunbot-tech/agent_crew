"""#322 — provider-neutral memory is shadow telemetry, never execution input."""
import asyncio
import inspect
import json
import os
import subprocess
import time
import uuid

from fastapi.testclient import TestClient

from agent_crew.memory import (
    FakeMemoryProvider,
    MemoryItem,
    MemoryRequest,
    NullMemoryProvider,
    MemoryResult,
    shadow_retrieve,
    shadow_telemetry,
)
from agent_crew.memory_runtime import (
    MemoryRecord, MemoryScope, RuntimeMemoryProvider, SQLiteMemoryStorage,
)
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _item(item_id, project, *, source_ref="git:abc", freshness="fresh"):
    return MemoryItem(
        item_id=item_id,
        project=project,
        memory_type="decision",
        source_ref=source_ref,
        created_at="2026-09-17T00:00:00Z",
        version_at="2026-09-17T00:00:00Z",
        freshness=freshness,
        excerpt="A prior decision that must remain shadow-only.",
    )


def test_null_and_fake_providers_are_deterministic_and_project_scoped():
    request = MemoryRequest(project="project-a", task_id="task-a", context_id="ctx-a")
    null = NullMemoryProvider()
    null_result = null.retrieve(request)
    assert null_result.provider == "null"
    assert null_result.state == "unavailable"
    assert null_result.items == ()

    fake = FakeMemoryProvider([_item("a", "project-a"), _item("b", "project-b")])
    result = fake.retrieve(request)
    assert [item.item_id for item in result.items] == ["a"]
    assert result.items[0].source_ref == "git:abc"
    assert result.items[0].freshness == "fresh"
    assert result.state == "results"


def test_fake_provider_default_denies_cross_project_memory_leakage():
    fake = FakeMemoryProvider([_item("only-b", "project-b")])
    result = fake.retrieve(MemoryRequest(project="project-a", task_id="task-a"))
    assert result.items == ()
    assert result.state == "empty"


class _BrokenProvider:
    name = "broken"
    backend = "test"

    def retrieve(self, request):
        raise RuntimeError("memory backend unavailable")


class _TimeoutProvider:
    name = "timeout"
    backend = "test"

    def retrieve(self, request):
        raise TimeoutError("memory lookup timed out")


def _dispatch_snapshot(tmp_path, monkeypatch, provider, *, shadow_memory_enabled=True,
                       shadow_memory_timeout_seconds=None, unused_tcp_port):
    """Run the production dispatch seam and return its actual provider prompt."""
    tmp_path.mkdir()
    wt = tmp_path / "claude"
    wt.mkdir()
    (wt / ".git").mkdir()
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"worktrees": {"claude": str(wt)}}))
    db = str(tmp_path / "tasks.db")
    spawned = {}

    async def fake_exec(*cmd, **kwargs):
        spawned["cmd"] = cmd

        class Process:
            returncode = 0
            pid = 1

            async def communicate(self):
                return (b"", b"")

            async def wait(self):
                return 0

        return Process()

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.delenv("AGENT_CREW_CONTEXT_PACK", raising=False)
    ids = iter(range(1, 1000))
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=next(ids)))
    monkeypatch.setattr("agent_crew.cea.engine.secrets.token_hex", lambda count: "a" * (2 * count))
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        "agent_crew.server.subprocess.run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, "", ""),
    )

    app = create_app(
        db_path=db, pane_map={}, port=unused_tcp_port, state_path=str(state_file), project="project-a",
        worktree_map={"implementer": str(wt)},
        memory_provider=provider, watchdog_disabled=True, anomaly_disabled=True,
        shadow_memory_enabled=shadow_memory_enabled,
        shadow_memory_timeout_seconds=shadow_memory_timeout_seconds,
    )
    with TestClient(app):
        queue = TaskQueue(db)
        queue.enqueue(TaskRequest(
            task_id="shadow-322", task_type="implement", description="make a change",
            branch="feat/322", project="project-a",
            context={"context_id": "task-context", "issue": 322},
        ))
        task = queue.dequeue(role="implementer")
        assert task is not None
        dispatch_started = time.perf_counter()
        asyncio.run(app.state.dispatch_task(task, "implementer"))
        dispatch_seconds = time.perf_counter() - dispatch_started

    events = [json.loads(line) for line in open(os.path.join(tmp_path, "context_events.jsonl"))]
    shadow = [event for event in events if event["event_type"] == "shadow_memory_retrieval"]
    return spawned["cmd"], TaskQueue(db).get_task_context("shadow-322"), shadow, dispatch_seconds


class _MustNotBeCalledProvider:
    name = "must-not-call"
    backend = "test"

    def retrieve(self, request):
        raise AssertionError("disabled shadow retrieval invoked the provider")


class _SlowProvider:
    name = "slow"
    backend = "test"

    def retrieve(self, request):
        time.sleep(2)
        return MemoryResult(provider=self.name, backend=self.backend, state="empty")


class _CrossProjectProvider:
    name = "cross-project"
    backend = "test"

    def retrieve(self, request):
        return MemoryResult(provider=self.name, backend=self.backend, state="results", items=(
            _item("project-a", "project-a"), _item("project-b", "project-b"),
            _item("project-less", ""),
        ))


def test_hybrid_dispatch_uses_ranked_memory_and_records_receipt(
        tmp_path, monkeypatch, *, unused_tcp_port):
    from agent_crew.memory_hybrid import HybridMemoryStorage
    from agent_crew.memory_runtime import MemoryRecord, MemoryScope

    path = tmp_path / "ranked.db"
    storage = HybridMemoryStorage(str(path))
    storage.put(MemoryRecord(
        "procedural", "owner_principle:fleet", {"text": "make a change"},
        MemoryScope(fleet="fleet")))
    storage.put(MemoryRecord(
        "decision", "standing:project", {"kind": "standing_decision", "text": "make a change"},
        MemoryScope(project="project-a")))
    monkeypatch.setenv("AGENT_CREW_MEMORY_BACKEND", "hybrid")
    monkeypatch.setenv("AGENT_CREW_MEMORY_DB", str(path))
    monkeypatch.setenv("AGENT_CREW_MEMORY_FLEET", "fleet")
    _, context, events, _ = _dispatch_snapshot(
        tmp_path / "dispatch", monkeypatch, _MustNotBeCalledProvider(),
        shadow_memory_enabled=False,
        unused_tcp_port=unused_tcp_port)
    assert len(events) == 1
    assert events[0]["retrieval_mode"] == "lexical_only"
    assert events[0]["head_bytes"] > 2
    assert events[0]["superseded_served"] == 0
    assert {"owner_principle:fleet", "standing:project"} <= set(events[0]["result_ids"])
    assert context["shadow_memory"]["retrieval_mode"] == "lexical_only"


def test_hybrid_dispatch_timeout_records_fallback_without_blocking(
        tmp_path, monkeypatch, *, unused_tcp_port):
    from agent_crew.memory_hybrid import HybridMemoryStorage
    from agent_crew.memory_runtime import MemoryRecord, MemoryScope

    path = tmp_path / "slow-ranked.db"
    storage = HybridMemoryStorage(str(path))
    storage.put(MemoryRecord("episodic", "slow", {"text": "make a change"},
                             MemoryScope(project="project-a")))
    monkeypatch.setenv("AGENT_CREW_MEMORY_BACKEND", "hybrid")
    monkeypatch.setenv("AGENT_CREW_MEMORY_DB", str(path))
    original_rank = HybridMemoryStorage._rank_middle

    def slow_rank(self, *args):
        time.sleep(.45)
        return original_rank(self, *args)

    monkeypatch.setattr(HybridMemoryStorage, "_rank_middle", slow_rank)
    _, context, events, elapsed = _dispatch_snapshot(
        tmp_path / "dispatch", monkeypatch, _MustNotBeCalledProvider(),
        unused_tcp_port=unused_tcp_port)
    assert elapsed < .6  # includes dispatch setup outside the 300 ms lookup
    assert events[0]["retrieval_mode"] == "fallback"
    assert events[0]["latency_ms"] == 300.0
    assert events[0]["superseded_served"] == 0
    assert context["shadow_memory"]["retrieval_mode"] == "fallback"


def test_shadow_memory_kill_switch_never_invokes_provider(tmp_path, monkeypatch, *, unused_tcp_port):
    _, context, events, _ = _dispatch_snapshot(
        tmp_path / "disabled", monkeypatch, _MustNotBeCalledProvider(), shadow_memory_enabled=False, unused_tcp_port=unused_tcp_port)

    assert "shadow_memory" not in context
    assert events == []


def test_existing_shadow_db_wires_runtime_provider_without_live_read_flag(
        tmp_path, monkeypatch, *, unused_tcp_port):
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    storage.put(MemoryRecord(
        "episodic", "episode-1", {"link": "git:episode-1", "topic": "prior result"},
        MemoryScope(project="project-a"),
    ))
    storage.put(MemoryRecord(
        "episodic", "unscoped-episode", {"topic": "unscoped result"}, MemoryScope(),
    ))
    monkeypatch.setenv("AGENT_CREW_SHADOW_MEMORY_DB", storage.path)
    monkeypatch.delenv("AGENT_CREW_ADR001_MEMORY_ENABLED", raising=False)
    cmd, _, events, _ = _dispatch_snapshot(
        tmp_path / "wired", monkeypatch, None, unused_tcp_port=unused_tcp_port)

    assert len(events) == 1
    assert events[0]["provider"] == "memory_runtime"
    assert events[0]["backend"] == "sqlite"
    assert events[0]["state"] == "results"
    assert events[0]["result_ids"] == ["episode-1"]
    assert events[0]["dropped_cross_project"] == 1
    assert "prior result" not in " ".join(map(str, cmd)).lower()


def test_missing_shadow_db_uses_null_provider_and_does_not_create_file(
        tmp_path, monkeypatch, caplog, *, unused_tcp_port):
    missing = tmp_path / "missing.db"
    monkeypatch.setenv("AGENT_CREW_SHADOW_MEMORY_DB", str(missing))
    _, _, events, _ = _dispatch_snapshot(
        tmp_path / "missing", monkeypatch, None, unused_tcp_port=unused_tcp_port)

    assert not missing.exists()
    assert events[0]["provider"] == "null"
    assert events[0]["state"] == "unavailable"
    assert sum("AGENT_CREW_SHADOW_MEMORY_DB" in record.message for record in caplog.records) == 1


def test_runtime_shadow_flag_off_makes_no_retrieval_and_keeps_message_identical(
        tmp_path, monkeypatch, *, unused_tcp_port):
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    storage.put(MemoryRecord(
        "episodic", "episode-1", {"topic": "prior result"}, MemoryScope(project="project-a"),
    ))
    monkeypatch.setenv("AGENT_CREW_SHADOW_MEMORY_DB", storage.path)
    monkeypatch.delenv("AGENT_CREW_ADR001_MEMORY_ENABLED", raising=False)
    calls = []
    original_retrieve = RuntimeMemoryProvider.retrieve

    def observed_retrieve(self, request):
        calls.append(request.task_id)
        return original_retrieve(self, request)

    monkeypatch.setattr(RuntimeMemoryProvider, "retrieve", observed_retrieve)
    off_cmd, off_context, off_events, _ = _dispatch_snapshot(
        tmp_path / "off", monkeypatch, None, shadow_memory_enabled=False,
        unused_tcp_port=unused_tcp_port)
    assert calls == []
    on_cmd, on_context, on_events, _ = _dispatch_snapshot(
        tmp_path / "on", monkeypatch, None, shadow_memory_enabled=True,
        unused_tcp_port=unused_tcp_port)

    assert off_cmd == on_cmd
    assert calls == ["shadow-322"]
    assert off_events == []
    assert "shadow_memory" not in off_context
    assert on_events[0]["result_ids"] == ["episode-1"]
    assert {key: value for key, value in on_context.items() if key != "shadow_memory"} == off_context


def test_slow_shadow_provider_cannot_hold_baseline_dispatch(tmp_path, monkeypatch, *, unused_tcp_port):
    _, _, events, dispatch_seconds = _dispatch_snapshot(
        tmp_path / "slow", monkeypatch, _SlowProvider(), shadow_memory_timeout_seconds=0.05, unused_tcp_port=unused_tcp_port)

    assert dispatch_seconds < 0.5
    assert events[0]["state"] == "timeout"


def test_shadow_dispatch_uses_250ms_default_timeout(tmp_path, monkeypatch, *, unused_tcp_port):
    monkeypatch.delenv("AGENT_CREW_SHADOW_MEMORY_TIMEOUT_SECONDS", raising=False)
    observed = []

    def bounded_retrieve(provider, request, timeout_seconds):
        observed.append(timeout_seconds)
        return MemoryResult(provider="fake", backend="test", state="empty")

    monkeypatch.setattr("agent_crew.server.shadow_retrieve_bounded", bounded_retrieve)
    _dispatch_snapshot(tmp_path / "default-budget", monkeypatch, FakeMemoryProvider([]),
                       unused_tcp_port=unused_tcp_port)
    assert observed == [0.25]


def test_shadow_retrieve_defense_in_depth_removes_cross_project_provider_items():
    result = shadow_retrieve(_CrossProjectProvider(), MemoryRequest(project="project-a"))

    assert result.state == "results"
    assert [item.item_id for item in result.items] == ["project-a"]
    assert result.dropped_cross_project == 2
    assert shadow_telemetry(result)["dropped_cross_project"] == 2


def test_shadow_results_leave_baseline_prompt_and_dispatch_byte_identical(tmp_path, monkeypatch, *, unused_tcp_port):
    baseline, baseline_context, baseline_events, _ = _dispatch_snapshot(
        tmp_path / "baseline", monkeypatch, NullMemoryProvider(), unused_tcp_port=unused_tcp_port)
    returned, returned_context, returned_events, _ = _dispatch_snapshot(
        tmp_path / "returned", monkeypatch,
        FakeMemoryProvider([_item("decision-1", "project-a")]),
        unused_tcp_port=unused_tcp_port,
    )

    assert returned == baseline
    assert "prior decision" not in " ".join(map(str, returned)).lower()
    assert ({key: value for key, value in returned_context.items() if key != "shadow_memory"}
            == {key: value for key, value in baseline_context.items() if key != "shadow_memory"})
    assert returned_events[0]["state"] == "results"
    assert returned_events[0]["result_ids"] == ["decision-1"]
    assert returned_events[0]["source_refs"] == ["git:abc"]
    assert baseline_events[0]["state"] == "unavailable"


def test_shadow_failures_timeouts_and_empty_results_leave_baseline_unchanged(tmp_path, monkeypatch, *, unused_tcp_port):
    baseline, _, _, _ = _dispatch_snapshot(tmp_path / "baseline", monkeypatch, NullMemoryProvider(), unused_tcp_port=unused_tcp_port)
    broken, _, broken_events, _ = _dispatch_snapshot(tmp_path / "broken", monkeypatch, _BrokenProvider(), unused_tcp_port=unused_tcp_port)
    timed_out, _, timeout_events, _ = _dispatch_snapshot(tmp_path / "timeout", monkeypatch, _TimeoutProvider(), unused_tcp_port=unused_tcp_port)
    empty, _, empty_events, _ = _dispatch_snapshot(
        tmp_path / "empty", monkeypatch, FakeMemoryProvider([]), unused_tcp_port=unused_tcp_port)

    assert broken == timed_out == empty == baseline
    assert broken_events[0]["state"] == "error"
    assert timeout_events[0]["state"] == "timeout"
    assert empty_events[0]["state"] == "empty"


def test_checkpoint_and_coordinator_recovery_are_structurally_memory_independent(tmp_path):
    import agent_crew.queue as queue_module

    recovery_source = "\n".join([
        inspect.getsource(TaskQueue.advance_coordinator),
        inspect.getsource(TaskQueue.get_coordinator_state),
        inspect.getsource(TaskQueue.export_project_runtime_state),
        inspect.getsource(queue_module),
    ])
    assert "agent_crew.memory" not in recovery_source
    assert "MemoryProvider" not in recovery_source

    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.advance_coordinator(coordinator_id="coord", generation=1, provider="provider")
    assert queue.get_coordinator_state()["coordinator_id"] == "coord"
    assert queue.export_project_runtime_state()["coordinator"]["checkpoint_ref"] == "coordinator::coord:1"
