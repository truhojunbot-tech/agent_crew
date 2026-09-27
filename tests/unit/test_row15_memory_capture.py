"""ADR row 1.5 capture remains evidence-only and project scoped."""
import json
import sqlite3
import hashlib

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app
from agent_crew.mcp_server import build_mcp_server
from agent_crew.memory_capture import (
    canonical_project, capture_blackboard_result, capture_episode, capture_task_outcome,
)
from agent_crew.memory_runtime import (
    MemoryScope, SQLiteMemoryStorage, capture_owner_statement, effective_owner_statements,
)
from scripts.migrate_memory_project_keys import migrate
from scripts.import_shadow_memory import import_file


def test_task_outcomes_write_canonical_evidence_without_promoting_procedure(tmp_path):
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    rows = capture_task_outcome(storage, project="alpha-engine", repo="alpha_engine",
                                task_id="review-1", status="needs_human",
                                summary="needs correction", verdict="request_changes")
    assert {r.layer for r in rows} == {"episodic", "decision", "failure_pattern"}
    assert {r.scope.project for r in rows} == {"alpha_engine"}
    assert {r.layer for r in storage.retrieve(MemoryScope(project="alpha_engine", task_id="review-1"))} == {
        "episodic", "decision", "failure_pattern"}
    assert all(r.layer != "procedural" for r in rows)
    completed = capture_task_outcome(storage, project="agent_crew", repo="agent_crew",
                                     task_id="impl-1", status="completed", summary="done")
    assert {row.layer for row in completed} == {"episodic", "decision"}


def test_blackboard_and_episode_import_map_project_and_reject_unknown(tmp_path):
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    entry = {"id": "board-1", "from": "Quota", "type": "result",
             "link": "https://github.com/example/quota-core/issues/4", "topic": "quota",
             "status": "done", "result_link": "https://github.com/example/quota-core/pull/5"}
    assert capture_blackboard_result(storage, entry).scope.project == "quota-core"
    assert capture_episode(storage, {"task_id": "t-1", "outcome": "failed",
                                     "summary": "broken"}, project="alpha-engine")[0].scope.project == "alpha_engine"
    with pytest.raises(ValueError):
        capture_blackboard_result(storage, {**entry, "id": "bad", "from": "unknown"})
    with pytest.raises(ValueError):
        canonical_project("Quota")
    assert all(row.scope.project for row in storage.retrieve(MemoryScope(project="quota-core")))


def test_import_rejects_unknown_project_with_telemetry(tmp_path, capsys):
    db = tmp_path / "memory.db"
    SQLiteMemoryStorage(str(db))
    source = tmp_path / "blackboard.jsonl"
    source.write_text(json.dumps({"id": "bad", "from": "unknown", "type": "result",
                                  "link": "https://example.test/unknown/issues/1",
                                  "topic": "x", "status": "done", "result_link": "x"}) + "\n")
    assert import_file(db, source, kind="blackboard") == {"rejected": 1}
    assert "shadow_memory_import_rejected" in capsys.readouterr().out
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM adr001_memory").fetchone()[0] == 0


def test_migration_dry_run_apply_creates_backup_and_no_duplicate(tmp_path):
    path = tmp_path / "memory.db"
    SQLiteMemoryStorage(str(path))
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO adr001_memory VALUES (?,?,?,?,?,?)", (
            "authoritative", "owner:alpha-engine:telegram:42:7", json.dumps({
                "kind": "owner_statement", "project": "alpha-engine"}),
            json.dumps({"project": "alpha-engine"}), 1, 1.0))
        db.commit()
    dry = migrate(path)
    assert dry["before"] == {"alpha-engine": 1}
    assert dry["after"] == {"alpha_engine": 1}
    assert dry["dry_run"] is True
    assert not (tmp_path / "memory.db.bak").exists()
    applied = migrate(path, apply=True)
    assert applied["backup"] == str(tmp_path / "memory.db.bak")
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT key,scope FROM adr001_memory").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "owner:alpha_engine:telegram:42:7"
    assert json.loads(rows[0][1])["project"] == "alpha_engine"
    with sqlite3.connect(tmp_path / "memory.db.bak") as db:
        assert db.execute("SELECT key FROM adr001_memory").fetchone()[0] == "owner:alpha-engine:telegram:42:7"


def test_migrated_owner_corrections_resolve_for_canonical_project(tmp_path):
    path = tmp_path / "memory.db"
    storage = SQLiteMemoryStorage(str(path))
    def proof(mid, text):
        return {"status": "VERIFIED", "message_id": mid, "user_id": "42",
                "chat_id": "42", "ts": "2026-09-27T00:00:00Z",
                "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
    first = capture_owner_statement(storage, project="alpha-engine",
                                    target_project="alpha-engine", proof=proof("7", "old"),
                                    text="old", source_ref="session.jsonl")
    capture_owner_statement(storage, project="alpha-engine", target_project="alpha-engine",
                            proof=proof("8", "new"), text="new", source_ref="session.jsonl",
                            supersedes=[first.key])
    migrate(path, apply=True)
    rows = effective_owner_statements(storage, "alpha_engine")
    assert len(rows) == 1
    assert rows[0].key == "owner:alpha_engine:telegram:42:8"
    assert rows[0].value["supersedes"] == ["owner:alpha_engine:telegram:42:7"]


@pytest.mark.parametrize("configured,broken", [(False, False), (True, False), (True, True)])
def test_result_capture_is_gated_and_never_changes_result(
        tmp_path, monkeypatch, configured, broken, unused_tcp_port):
    memory_db = tmp_path / "memory.db"
    SQLiteMemoryStorage(str(memory_db))
    if configured:
        monkeypatch.setenv("AGENT_CREW_SHADOW_MEMORY_DB", str(memory_db))
    else:
        monkeypatch.delenv("AGENT_CREW_SHADOW_MEMORY_DB", raising=False)
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "shadow")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    if broken:
        def fail_capture(*args, **kwargs):
            raise RuntimeError("capture unavailable")
        monkeypatch.setattr("agent_crew.memory_capture.capture_task_outcome", fail_capture)
    db = str(tmp_path / "tasks.db")
    app = create_app(db, pane_map={}, port=unused_tcp_port, project="agent_crew",
                     watchdog_disabled=True, anomaly_disabled=True, worktree_map={})
    with TestClient(app) as api:
        queue = TaskQueue(db)
        queue.enqueue(TaskRequest(task_id="capture-test", task_type="discuss",
                                  description="discussion", project="agent_crew"))
        response = api.post("/tasks/capture-test/result", json={
            "task_id": "capture-test", "status": "failed", "summary": "observed failure"})
        assert response.status_code == 200, response.text
        assert queue.get_task_status("capture-test") == "failed"
    with sqlite3.connect(memory_db) as db_conn:
        count = db_conn.execute("SELECT count(*) FROM adr001_memory").fetchone()[0]
    assert count == (0 if broken or not configured else 3)
    if configured:
        events = [json.loads(line) for line in (tmp_path / "context_events.jsonl").read_text().splitlines()]
        captures = [event for event in events if event["event_type"] == "shadow_memory_capture"]
        assert captures[-1]["outcome"] == ("rejected" if broken else "stored")


def test_mcp_result_uses_same_capture_path(tmp_path, monkeypatch):
    memory_db = tmp_path / "memory.db"
    SQLiteMemoryStorage(str(memory_db))
    monkeypatch.setenv("AGENT_CREW_SHADOW_MEMORY_DB", str(memory_db))
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "shadow")
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest(task_id="mcp-capture", task_type="discuss",
                              description="discussion", project="agent_crew"))
    mcp = build_mcp_server(db)
    response = mcp._tool_manager._tools["submit_result"].fn(
        task_id="mcp-capture", status="needs_human", summary="needs owner")
    assert response["acknowledged"] is True
    assert queue.get_task_status("mcp-capture") == "needs_human"
    with sqlite3.connect(memory_db) as connection:
        assert connection.execute("SELECT count(*) FROM adr001_memory").fetchone()[0] == 3
