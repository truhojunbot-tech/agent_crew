"""#322 — provider-neutral memory is shadow telemetry, never execution input."""
import asyncio
import inspect
import json
import os
import subprocess
import time

from fastapi.testclient import TestClient

from agent_crew.memory import (
    FakeMemoryProvider,
    MemoryItem,
    MemoryRequest,
    NullMemoryProvider,
    MemoryResult,
    shadow_retrieve,
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
                       shadow_memory_timeout_seconds=None):
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
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.delenv("AGENT_CREW_CONTEXT_PACK", raising=False)
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        "agent_crew.server.subprocess.run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, "", ""),
    )

    app = create_app(
        db_path=db, pane_map={}, port=0, state_path=str(state_file), project="project-a",
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
        ))


def test_shadow_memory_kill_switch_never_invokes_provider(tmp_path, monkeypatch):
    _, context, events, _ = _dispatch_snapshot(
        tmp_path / "disabled", monkeypatch, _MustNotBeCalledProvider(), shadow_memory_enabled=False)

    assert "shadow_memory" not in context
    assert events == []


def test_slow_shadow_provider_cannot_hold_baseline_dispatch(tmp_path, monkeypatch):
    _, _, events, dispatch_seconds = _dispatch_snapshot(
        tmp_path / "slow", monkeypatch, _SlowProvider(), shadow_memory_timeout_seconds=0.05)

    assert dispatch_seconds < 0.5
    assert events[0]["state"] == "timeout"


def test_shadow_retrieve_defense_in_depth_removes_cross_project_provider_items():
    result = shadow_retrieve(_CrossProjectProvider(), MemoryRequest(project="project-a"))

    assert result.state == "results"
    assert [item.item_id for item in result.items] == ["project-a"]


def test_shadow_results_leave_baseline_prompt_and_dispatch_byte_identical(tmp_path, monkeypatch):
    baseline, baseline_context, baseline_events, _ = _dispatch_snapshot(
        tmp_path / "baseline", monkeypatch, NullMemoryProvider())
    returned, returned_context, returned_events, _ = _dispatch_snapshot(
        tmp_path / "returned", monkeypatch,
        FakeMemoryProvider([_item("decision-1", "project-a")]),
    )

    assert returned == baseline
    assert "prior decision" not in " ".join(map(str, returned)).lower()
    assert ({key: value for key, value in returned_context.items() if key != "shadow_memory"}
            == {key: value for key, value in baseline_context.items() if key != "shadow_memory"})
    assert returned_events[0]["state"] == "results"
    assert returned_events[0]["result_ids"] == ["decision-1"]
    assert returned_events[0]["source_refs"] == ["git:abc"]
    assert baseline_events[0]["state"] == "unavailable"


def test_shadow_failures_timeouts_and_empty_results_leave_baseline_unchanged(tmp_path, monkeypatch):
    baseline, _, _, _ = _dispatch_snapshot(tmp_path / "baseline", monkeypatch, NullMemoryProvider())
    broken, _, broken_events, _ = _dispatch_snapshot(tmp_path / "broken", monkeypatch, _BrokenProvider())
    timed_out, _, timeout_events, _ = _dispatch_snapshot(tmp_path / "timeout", monkeypatch, _TimeoutProvider())
    empty, _, empty_events, _ = _dispatch_snapshot(
        tmp_path / "empty", monkeypatch, FakeMemoryProvider([]))

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
