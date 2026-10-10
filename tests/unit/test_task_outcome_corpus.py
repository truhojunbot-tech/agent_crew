"""Task outcomes are reusable ADR-001 evidence, including after a backfill."""
import json
import sqlite3

from agent_crew.memory_capture import (backfill_task_outcomes, capture_task_outcome_record,
                                       capture_result_best_effort, capture_merge_best_effort)
from agent_crew.memory_runtime import MemoryScope, SQLiteMemoryStorage
from scripts.backfill_task_outcomes import count_available_refs


def task_db_at(path):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, task_type TEXT, "
                   "description TEXT, branch TEXT, context TEXT, status TEXT, "
                   "created_at REAL, project TEXT, summary TEXT, verdict TEXT, "
                   "findings TEXT, pr_number INTEGER)")


def test_review_outcome_contains_findings_and_root_identity(tmp_path):
    task_db = tmp_path / "tasks.db"
    task_db_at(task_db)
    with sqlite3.connect(task_db) as db:
        db.execute("INSERT INTO tasks(task_id,task_type,description,branch,context,status,created_at,project,summary,verdict,findings,pr_number) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("impl-1", "implement", "Issue 42", "b", '{"issue":42}', "completed", 1,
                    "agent_crew", "Implemented", None, None, 7))
        db.execute("INSERT INTO tasks(task_id,task_type,description,branch,context,status,created_at,project,summary,verdict,findings,pr_number) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("review-1", "review", "Review PR #7", "b", '{"prev_task_id":"impl-1","pr_number":7,"pr_title":"Fix guard"}',
                    "completed", 2, "agent_crew", "Found a gap", "request_changes",
                    '[{"severity":"HIGH","description":"missing guard"}]', 7))
    memory = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    record = capture_task_outcome_record(memory, str(task_db), "review-1")
    assert record.key == "task_outcome:agent_crew:review-1"
    assert record.value["kind"] == "task_outcome"
    assert record.value["lineage_root_task_id"] == "impl-1"
    assert record.value["issue"] == "42"
    assert record.value["findings"][0]["description"] == "missing guard"
    assert record.value["pr_number"] == 7
    assert record.value["pr_title"] == "Fix guard"
    assert record.scope == MemoryScope(project="agent_crew")
    assert memory.retrieve(MemoryScope(project="agent_crew", task_id="later"), exact_key=record.key)


def test_backfill_is_idempotent_and_requires_terminal_evidence(tmp_path):
    task_db = tmp_path / "tasks.db"
    task_db_at(task_db)
    with sqlite3.connect(task_db) as db:
        db.executemany("INSERT INTO tasks(task_id,task_type,description,branch,context,status,created_at,project,summary,verdict,findings,pr_number) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
            ("review-1", "review", "Review PR #7", "b", '{"issue":42}', "completed", 1, "agent_crew", "approved", "approve", "[]", 7),
            ("pending-1", "review", "Review PR #8", "b", '{}', "pending", 2, "agent_crew", None, None, None, 8),
        ])
    memory = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    assert backfill_task_outcomes(memory, [str(task_db)]) == 1
    with sqlite3.connect(memory.path) as db:
        first = db.execute("SELECT key,value,version FROM adr001_memory WHERE key LIKE 'task_outcome:%'").fetchall()
    assert backfill_task_outcomes(memory, [str(task_db)]) == 0
    with sqlite3.connect(memory.path) as db:
        second = db.execute("SELECT key,value,version FROM adr001_memory WHERE key LIKE 'task_outcome:%'").fetchall()
    assert first == second


def test_ref_audit_uses_project_and_source_identity(tmp_path):
    memory = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    from agent_crew.memory_runtime import MemoryRecord
    memory.put_many_shadow([MemoryRecord("episodic", "task_outcome:agent_crew:review-1",
        {"kind": "task_outcome", "project": "agent_crew", "task_id": "review-1",
         "issue": "42", "pr_number": 7}, MemoryScope(project="agent_crew"))])
    eval_set = tmp_path / "eval.json"
    eval_set.write_text(json.dumps({"task_cases": [
        {"case_id": "yes", "project": "agent_crew", "expected": [
            {"source_kind": "issue_or_pr", "ref": "agent_crew PR #7 (merged)"}]},
        {"case_id": "wrong-project", "project": "other", "expected": [
            {"source_kind": "issue_or_pr", "ref": "other PR #7 (merged)"}]},
        {"case_id": "finding", "project": "agent_crew", "expected": [
            {"source_kind": "lineage_finding", "ref": "review-1 (PR #7 review)"}]},
    ]}))
    hits, total, missing = count_available_refs(memory.path, str(eval_set))
    assert (hits, total) == (2, 3)
    assert missing[0][0] == "wrong-project"


def test_live_review_capture_refreshes_after_merge(tmp_path, monkeypatch):
    task_db = tmp_path / "tasks.db"
    task_db_at(task_db)
    with sqlite3.connect(task_db) as db:
        db.execute("CREATE TABLE external_op (op_key TEXT PRIMARY KEY, state TEXT)")
        db.execute("INSERT INTO tasks(task_id,task_type,description,branch,context,status,created_at,project,summary,verdict,findings,pr_number) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("review-1", "review", "Review PR #7", "b", '{"issue":42}', "completed", 1,
                    "agent_crew", "approved", "approve", "[]", 7))
    memory = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    monkeypatch.setenv("AGENT_CREW_SHADOW_MEMORY_DB", memory.path)
    result = type("Result", (), {"status": "completed", "summary": "approved",
                                   "verdict": "approve", "pr_number": 7})()
    capture_result_best_effort(str(task_db), "review-1", result)
    key = "task_outcome:agent_crew:review-1"
    assert memory.retrieve(MemoryScope(project="agent_crew"), exact_key=key)[0].value["merge_state"] == ""
    with sqlite3.connect(task_db) as db:
        db.execute("INSERT INTO external_op VALUES (?,?)", ("merge:pr:7", "done"))
    capture_merge_best_effort(str(task_db), "review-1")
    rows = memory.retrieve(MemoryScope(project="agent_crew"), exact_key=key)
    assert len(rows) == 1
    assert rows[0].value["merge_state"] == "merged"
