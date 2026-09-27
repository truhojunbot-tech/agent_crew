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
    capture_result_best_effort,
)
from agent_crew.memory_runtime import (
    MemoryScope, SQLiteMemoryStorage, RuntimeMemoryProvider, MemoryRecord,
    capture_owner_statement, effective_owner_statements, reconstruct_context,
)
from agent_crew.memory import MemoryRequest, shadow_retrieve
from scripts.migrate_memory_project_keys import migrate
import scripts.migrate_memory_project_keys as migration_module
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


def test_runtime_returns_new_layers_and_drops_projectless_decision(tmp_path):
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    capture_task_outcome(storage, project="agent_crew", repo="agent_crew",
                         task_id="mine", status="failed", summary="failed")
    storage.put(MemoryRecord("decision", "broad", {}, MemoryScope()))
    result = shadow_retrieve(RuntimeMemoryProvider(storage), MemoryRequest(project="agent_crew"))
    assert {item.memory_type for item in result.items} == {
        "episodic", "decision", "failure_pattern"}
    assert "broad" not in {item.item_id for item in result.items}
    assert result.dropped_cross_project == 1


def test_runtime_drops_foreign_decision_from_provider():
    class UnscopedStorage:
        def retrieve(self, scope, query="", exact_key=""):
            return [MemoryRecord("decision", "foreign", {}, MemoryScope(project="halla")),
                    MemoryRecord("decision", "mine", {}, MemoryScope(project="agent_crew"))]
    result = shadow_retrieve(RuntimeMemoryProvider(UnscopedStorage()),
                             MemoryRequest(project="agent_crew"))
    assert [item.item_id for item in result.items] == ["mine"]
    assert result.dropped_cross_project == 1


def test_capture_retains_bounded_project_rows_and_read_is_limited(tmp_path):
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    for number in range(320):
        capture_task_outcome(storage, project="agent_crew", repo="agent_crew",
                             task_id=f"task-{number}", status="failed", summary="failed")
    with sqlite3.connect(storage.path) as db:
        assert db.execute("SELECT count(*) FROM adr001_memory").fetchone()[0] <= 900
    result = RuntimeMemoryProvider(storage).retrieve(MemoryRequest(project="agent_crew", limit=10))
    assert len(result.items) == 10


def test_shadow_read_parses_only_bounded_rows_with_large_existing_db(tmp_path, monkeypatch):
    import agent_crew.memory_runtime as runtime
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    with sqlite3.connect(storage.path) as db:
        db.executemany("INSERT INTO adr001_memory VALUES (?,?,?,?,?,?)", [
            ("decision", f"historical-{i}", "{}", json.dumps({"project": "agent_crew"}), 1, i)
            for i in range(2500)])
        db.commit()
    count = 0
    original = json.loads

    def counted(value, *args, **kwargs):
        nonlocal count
        count += 1
        return original(value, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(runtime.json, "loads", counted)
        result = RuntimeMemoryProvider(storage).retrieve(MemoryRequest(project="agent_crew", limit=10))
    assert len(result.items) == 10
    assert count <= 30


def test_live_read_excludes_captured_layers(tmp_path, monkeypatch):
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    capture_task_outcome(storage, project="agent_crew", repo="agent_crew",
                         task_id="mine", status="failed", summary="failed")
    monkeypatch.setenv("AGENT_CREW_ADR001_MEMORY_ENABLED", "1")
    records = reconstruct_context(storage, "reviewer", "other",
                                  MemoryScope(project="agent_crew"))["records"]
    assert not {"episodic", "decision", "failure_pattern"}.intersection(
        record["layer"] for record in records)


def test_revised_outcome_replaces_first_capture(tmp_path):
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    capture_task_outcome(storage, project="agent_crew", repo="agent_crew",
                         task_id="reused", status="failed", summary="first")
    capture_task_outcome(storage, project="agent_crew", repo="agent_crew",
                         task_id="reused", status="completed", summary="corrected")
    records = storage.retrieve(MemoryScope(project="agent_crew"))
    assert {r.value["status"] for r in records} == {"completed"}
    assert {r.layer for r in records} == {"episodic", "decision"}


def test_timeout_is_captured_as_failure_evidence(tmp_path):
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    rows = capture_task_outcome(storage, project="agent_crew", repo="agent_crew",
                                task_id="timeout", status="timed_out", summary="late")
    assert {row.layer for row in rows} == {"episodic", "decision", "failure_pattern"}


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
    assert import_file(db, source, kind="blackboard") == {
        "stored": 0, "rejected": 1, "dry_run": True}
    assert "shadow_memory_import_rejected" in capsys.readouterr().out
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM adr001_memory").fetchone()[0] == 0


def test_import_defaults_to_dry_run(tmp_path):
    db = tmp_path / "memory.db"
    SQLiteMemoryStorage(str(db))
    source = tmp_path / "blackboard.jsonl"
    source.write_text(json.dumps({"id": "good", "from": "alpha-engine", "type": "result",
                                  "link": "https://example.test/alpha_engine/issues/1", "topic": "x",
                                  "status": "done", "result_link": "x"}) + "\n")
    assert import_file(db, source, kind="blackboard") == {
        "stored": 1, "rejected": 0, "dry_run": True}
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM adr001_memory").fetchone()[0] == 0
    assert import_file(db, source, kind="blackboard", apply=True)["stored"] == 1


def test_capture_setup_failure_cannot_raise_after_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_SHADOW_MEMORY_DB", str(tmp_path / "memory.db"))
    with monkeypatch.context() as patch:
        patch.setattr("agent_crew.memory_capture.Path.resolve",
                      lambda self: (_ for _ in ()).throw(OSError("resolve unavailable")))
        capture_result_best_effort(str(tmp_path / "tasks.db"), "task",
                                   type("Result", (), {"status": "failed"})())


def test_additional_known_projects_are_canonical():
    assert canonical_project("agent_council") == "agent_council"
    assert canonical_project("apify-forge") == "apify-forge"
    assert canonical_project("claude_autonomous_trader") == "claude_autonomous_trader"
    assert canonical_project("ht-8004") == "ht-8004"


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


def test_refused_migration_removes_its_backup_so_retry_can_apply(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    SQLiteMemoryStorage(str(path))
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO adr001_memory VALUES (?,?,?,?,?,?)", (
            "authoritative", "owner:alpha-engine:telegram:42:7", "{}",
            json.dumps({"project": "alpha-engine"}), 1, 1.0))
        db.commit()
    original = migration_module.plan
    calls = 0

    def fail_second_plan(db):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("changed during backup")
        return original(db)

    with monkeypatch.context() as patch:
        patch.setattr(migration_module, "plan", fail_second_plan)
        with pytest.raises(RuntimeError):
            migrate(path, apply=True)
    assert not (tmp_path / "memory.db.bak").exists()
    assert migrate(path, apply=True)["dry_run"] is False


@pytest.mark.parametrize("configured,broken,status", [
    (False, False, "failed"), (True, False, "failed"),
    (True, True, "failed"), (True, False, "timed_out")])
def test_result_capture_is_gated_and_never_changes_result(
        tmp_path, monkeypatch, configured, broken, status, unused_tcp_port):
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
            "task_id": "capture-test", "status": status, "summary": "observed failure"})
        assert response.status_code == 200, response.text
        assert queue.get_task_status("capture-test") == status
    with sqlite3.connect(memory_db) as db_conn:
        count = db_conn.execute("SELECT count(*) FROM adr001_memory").fetchone()[0]
    assert count == (0 if broken or not configured else 3)
    if configured:
        events = [json.loads(line) for line in (tmp_path / "context_events.jsonl").read_text().splitlines()]
        captures = [event for event in events if event["event_type"] == "shadow_memory_capture"]
        assert captures[-1]["outcome"] == ("rejected" if broken else "stored")


def test_independent_capture_switch_returns_before_db_access(tmp_path, monkeypatch):
    memory_db = tmp_path / "memory.db"
    SQLiteMemoryStorage(str(memory_db))
    monkeypatch.setenv("AGENT_CREW_SHADOW_MEMORY_DB", str(memory_db))
    monkeypatch.setenv("AGENT_CREW_SHADOW_MEMORY_CAPTURE_ENABLED", "0")
    capture_result_best_effort(str(tmp_path / "missing-tasks.db"), "task",
                               type("Result", (), {"status": "failed"})())
    with sqlite3.connect(memory_db) as db:
        assert db.execute("SELECT count(*) FROM adr001_memory").fetchone()[0] == 0


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
