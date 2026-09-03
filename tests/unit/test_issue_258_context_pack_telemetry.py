"""#258 — the Context Pack recorded no telemetry at all, silently.

`record_context_event(..., role=role, **pack.telemetry())` raised
`TypeError: got multiple values for keyword argument 'role'` on every dispatch,
because `telemetry()` already carries `role`. The surrounding
`except Exception: logger.exception(...)` caught it, so the dispatcher kept
working and the feature recorded nothing — for months, and it took a consumer in
another repo (quota-core) building against the contract to notice.

Two kinds of test, because the bug had two properties:

  * it was a *collision*, so one test makes collisions structurally impossible
    to reintroduce rather than merely absent today;
  * it was *silent*, so another drives the real dispatch and asserts the event
    actually lands on disk — which is the only check the original feature was
    missing.
"""

import json
import os

import pytest

from agent_crew.context_identity import record_context_event
from agent_crew.context_pack import ContextPack

#: What the dispatcher names explicitly when recording `context_pack_built`.
EXPLICIT_KWARGS = {"task_id", "project", "role", "agent",
                   "context_id", "context_generation"}


def test_telemetry_keys_do_not_collide_with_the_explicit_fields():
    """★The bug, reduced: `telemetry()` owns `role`, and so did the call site.

    Kept as a contract test on the two key sets. `telemetry()` is meant to grow
    — #239 documents it as the pack's own schema — and every future key is a
    chance to silently switch this event off again.
    """
    telemetry_keys = set(ContextPack(task_id="t", role="implementer",
                                     mode="lexical", budget={}).telemetry())

    overlap = telemetry_keys & EXPLICIT_KWARGS
    assert overlap == {"role"}, (
        "the overlap changed; the dispatcher merges these into one dict, so a "
        f"new collision is safe, but this test should be updated: {overlap}"
    )


def test_recording_the_event_the_old_way_still_raises(tmp_path):
    """Pins WHY the call site is written as a merge, so nobody 'simplifies' it
    back into a splat alongside explicit kwargs."""
    pack = ContextPack(task_id="t-1", role="implementer", mode="lexical", budget={})
    path = str(tmp_path / "events.jsonl")

    with pytest.raises(TypeError, match="multiple values for keyword argument 'role'"):
        record_context_event(path, "context_pack_built", task_id="t-1",
                             project="p", role="implementer", agent="claude",
                             context_id="c", context_generation=1,
                             **pack.telemetry())


def test_the_merged_form_records_cleanly(tmp_path):
    pack = ContextPack(task_id="t-1", role="implementer", mode="lexical", budget={})
    path = str(tmp_path / "events.jsonl")

    fields = {"task_id": "t-1", "project": "p", "role": "implementer",
              "agent": "claude", "context_id": "c", "context_generation": 1}
    fields.update(pack.telemetry())
    record_context_event(path, "context_pack_built", **fields)

    event = json.loads(open(path).read().strip())
    assert event["event_type"] == "context_pack_built"
    assert event["role"] == "implementer"
    assert event["context_pack_id"] == pack.pack_id


def test_a_real_dispatch_writes_the_event(tmp_path, monkeypatch):
    """★★The check the feature never had.

    #239 tested that the pack reached the prompt; nothing tested that the
    dispatch recorded it. A TypeError one line later was therefore invisible.
    Drives `_dispatch_task` with the pack ENABLED and reads the events file.
    """
    import asyncio

    from fastapi.testclient import TestClient

    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue
    from agent_crew.server import create_app

    wt = tmp_path / "claude"
    wt.mkdir()
    (wt / ".git").mkdir()
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"worktrees": {"claude": str(wt)}}))
    db = str(tmp_path / "t.db")

    async def _fake_exec(*cmd, **kwargs):
        class _P:
            returncode = 0
            pid = 1

            async def communicate(self):
                return (b"", b"")

            async def wait(self):
                return 0

        return _P()

    monkeypatch.setenv("AGENT_CREW_CONTEXT_PACK", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec", _fake_exec)

    app = create_app(db_path=db, pane_map={}, port=0, state_path=str(state_file),
                     project="agent_crew", watchdog_disabled=True,
                     anomaly_disabled=True)
    with TestClient(app):
        q = TaskQueue(db)
        q.enqueue(TaskRequest(
            task_id="disp-tel", task_type="implement", description="do it",
            branch="main",
            context={"issue": 42, "repo": "org/repo", "issue_title": "t",
                     "issue_body": "## Acceptance criteria\n- [ ] works\n"}))
        task = q.dequeue(role="implementer")
        assert task is not None
        asyncio.run(app.state.dispatch_task(task, "implementer"))

    events_path = os.path.join(os.path.dirname(db), "context_events.jsonl")
    events = [json.loads(line) for line in open(events_path)]
    built = [e for e in events if e["event_type"] == "context_pack_built"]

    assert built, (
        "no context_pack_built event was recorded — the feature is enabled and "
        "silent, which is exactly the state #258 found"
    )
    event = built[0]
    assert event["task_id"] == "disp-tel"
    assert event["role"] == "implementer"
    assert event["agent"] == "claude"
    # ...and the consumer-facing schema quota-core builds against.
    for key in ("context_pack_id", "context_pack_hash", "context_pack_schema_version",
                "mode", "total_tokens", "selected_count", "candidate_count",
                "degraded", "budget"):
        assert key in event, f"consumer schema field missing from telemetry: {key}"


def test_the_dispatch_records_nothing_when_the_pack_is_disabled(tmp_path, monkeypatch):
    """The event marks a pack that was actually built — an off feature emits
    no telemetry rather than an empty record."""
    import asyncio

    from fastapi.testclient import TestClient

    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue
    from agent_crew.server import create_app

    wt = tmp_path / "claude"
    wt.mkdir()
    (wt / ".git").mkdir()
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"worktrees": {"claude": str(wt)}}))
    db = str(tmp_path / "t.db")

    async def _fake_exec(*cmd, **kwargs):
        class _P:
            returncode = 0
            pid = 1

            async def communicate(self):
                return (b"", b"")

            async def wait(self):
                return 0

        return _P()

    monkeypatch.delenv("AGENT_CREW_CONTEXT_PACK", raising=False)
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setattr("agent_crew.server.asyncio.create_subprocess_exec", _fake_exec)

    app = create_app(db_path=db, pane_map={}, port=0, state_path=str(state_file),
                     project="agent_crew", watchdog_disabled=True,
                     anomaly_disabled=True)
    with TestClient(app):
        q = TaskQueue(db)
        q.enqueue(TaskRequest(task_id="disp-off", task_type="implement",
                              description="do it", branch="main", context={}))
        task = q.dequeue(role="implementer")
        asyncio.run(app.state.dispatch_task(task, "implementer"))

    events_path = os.path.join(os.path.dirname(db), "context_events.jsonl")
    events = [json.loads(line) for line in open(events_path)]
    assert not [e for e in events if e["event_type"] == "context_pack_built"]
