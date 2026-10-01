"""Opt-in Codex session renewal keeps fix rounds on their existing session."""
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_crew import server as sv
from agent_crew.memory_runtime import SQLiteMemoryStorage, capture_owner_statement
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


@pytest.mark.parametrize("summary", ["Condensed previous work", ""])
def test_rollout_summary_or_checkpoint_only(tmp_path, summary):
    folder = tmp_path / "sessions" / "2026" / "09" / "27"
    folder.mkdir(parents=True)
    path = folder / "rollout-2026-09-27T01-00-00-old.jsonl"
    path.write_text(json.dumps({"type": "compacted", "payload": {
        "message": summary, "replacement_history": [{"type": "compaction",
                                                  "encrypted_content": "opaque"}]}}) + "\n")
    assert sv.codex_latest_compaction_summary("old", home=tmp_path) == summary


@pytest.mark.parametrize("payload,expected", [
    ({"message": {"content": [{"text": "First part"}, {"text": "Second part"}]}},
     "First part\nSecond part"),
    ({"message": "", "replacement_history": [
        {"type": "compaction", "text": "Earlier summary"},
        {"type": "compaction", "text": "Latest summary"}]}, "Latest summary"),
])
def test_rollout_summary_structured_message_and_history_fallback(tmp_path, payload, expected):
    folder = tmp_path / "sessions" / "2026" / "09" / "27"
    folder.mkdir(parents=True)
    (folder / "rollout-2026-09-27T01-00-00-old.jsonl").write_text(
        json.dumps({"type": "compacted", "payload": payload}) + "\n")
    assert sv.codex_latest_compaction_summary("old", home=tmp_path) == expected


def test_codex_output_session_id():
    assert sv.codex_thread_id_from_output(
        '{"type":"thread.started","thread_id":"new-session"}\n') == "new-session"


def test_codex_output_session_id_skips_malformed_lines():
    assert sv.codex_thread_id_from_output(
        '{"type":"thread.started", bad json\n'
        '{"type":"thread.started","thread_id":"valid"}\n') == "valid"


def test_codex_output_session_id_missing_id_is_empty():
    assert sv.codex_thread_id_from_output('{"type":"thread.started"}\n') == ""


def test_codex_output_session_id_uses_first_started_event():
    assert sv.codex_thread_id_from_output(
        '{"type":"thread.started","thread_id":"first"}\n'
        '{"type":"thread.started","thread_id":"second"}\n') == "first"


@pytest.mark.parametrize("mode,task_type,context,summary,expected_resume,owner_text", [
    ("", "implement", {}, "Previous decisions summary", True, None),
    ("renew_rehydrate", "implement", {}, "Previous decisions summary", False, None),
    ("renew_rehydrate", "implement", {}, "", False, None),
    ("renew_rehydrate", "implement", {}, "Previous decisions summary", False, "Owner approved the plan"),
    ("renew_rehydrate", "implement", {"fix_round": 1}, "Previous decisions summary", True, None),
])
def test_dispatch_renewal(tmp_path, monkeypatch, unused_tcp_port,
                          mode, task_type, context, summary, expected_resume, owner_text):
    wt = tmp_path / "codex"
    wt.mkdir()
    (wt / ".git").mkdir()
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"worktrees": {"codex": str(wt)},
                                 "codex_session_mode": mode}))
    db = str(tmp_path / "tasks.db")
    commands = []

    async def fake_exec(*cmd, **kwargs):
        commands.append(list(cmd))
        if len(commands) == 2 and not expected_resume:
            session[0] = "new"
        class Process:
            returncode = 0
            pid = 1
            async def communicate(self): return (b"", b"")
            async def wait(self): return 0
        return Process()

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    monkeypatch.setattr(sv.asyncio, "create_subprocess_exec", fake_exec)
    session = ["old"]
    monkeypatch.setattr(sv, "codex_session_for_cwd", lambda *args, **kw: session[0])
    monkeypatch.setattr(sv, "codex_latest_compaction_summary",
                        lambda *args, **kw: summary)
    memory_provider = None
    if owner_text:
        storage = SQLiteMemoryStorage(str(tmp_path / "owner.db"))
        capture_owner_statement(
            storage, project="p", target_project="p", text=owner_text,
            source_ref="session.jsonl",
            proof={"status": "VERIFIED", "message_id": "7", "user_id": "42",
                   "chat_id": "42", "ts": "2026-09-26T00:00:00Z",
                   "text_sha256": hashlib.sha256(owner_text.encode()).hexdigest()},
        )
        memory_provider = SimpleNamespace(storage=storage)
    app = sv.create_app(db_path=db, pane_map={}, port=unused_tcp_port,
                        state_path=str(state), project="p", watchdog_disabled=True,
                        anomaly_disabled=True, memory_provider=memory_provider,
                        shadow_memory_enabled=False)
    with TestClient(app):
        queue = TaskQueue(db)
        for tid, ctx in (("first", {}), ("second", context)):
            queue.enqueue(TaskRequest(task_id=tid, task_type=task_type,
                                      description="Do work", branch="feat/test",
                                      project="p", context=ctx))
            task = queue.dequeue(role="implementer")
            assert task is not None
            asyncio.run(app.state.dispatch_task(task, "implementer"))
        cmd = commands[-1]
        assert ("resume" in cmd) == expected_resume
        if not expected_resume:
            assert ("Previous decisions summary" in cmd[-1]) == bool(summary)
            assert "ADR-001 7.7 renewal checkpoint" in cmd[-1]
            assert "feat/test" in cmd[-1]
            checkpoint = json.loads(cmd[-1].split("\n", 1)[1].split("\n", 1)[0])
            assert checkpoint["owner_authority_facts"] == (
                [{"source_ref": "session.jsonl", "text": owner_text}]
                if owner_text else [])
            assert queue.get_task_context("second")["context_renewal"]["seed"] == (
                "summary_and_checkpoint" if summary else "checkpoint_only")
            assert queue.get_task_context("second")["context_renewal"]["new_session_id"] == "new"
            assert queue.get_attribution("second")["provider_session_id"] == "new"
            events = [json.loads(line) for line in
                      (tmp_path / "context_events.jsonl").read_text().splitlines()]
            assert any(event["event_type"] == "context_renewed" and
                       event["previous_session_id"] == "old" for event in events)
        else:
            assert "Previous decisions summary" not in cmd[-1]
