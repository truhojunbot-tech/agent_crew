"""#590: lineage recall and bounded shadow-memory reads stay observational."""

import json
import sqlite3

from agent_crew.memory import (FakeMemoryProvider, MemoryItem, MemoryRequest,
                               shadow_retrieve_bounded)
from agent_crew.memory_capture import (capture_result_best_effort, capture_task_outcome,
                                       task_lineage)
from agent_crew.memory_runtime import (MemoryRecord, MemoryScope, RuntimeMemoryProvider,
                                       SQLiteMemoryStorage)
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import _lineage_recall_observation
from tests.unit.test_issue_322_memory_provider import _dispatch_snapshot


def _task(task_id, *, prev="", pr=590):
    return TaskRequest(
        task_id=task_id, task_type="implement", description=task_id,
        branch="feature/590", project="agent_crew", pr_number=pr,
        context={"prev_task_id": prev, "pr_number": pr} if prev else {"pr_number": pr},
    )


def test_fix_retrieves_review_and_implement_predecessors(tmp_path, monkeypatch):
    db_path = str(tmp_path / "tasks.db")
    memory_path = str(tmp_path / "memory.db")
    storage = SQLiteMemoryStorage(memory_path)
    monkeypatch.setenv("AGENT_CREW_SHADOW_MEMORY_DB", memory_path)
    q = TaskQueue(db_path)
    for task in (_task("impl-root"), _task("review-root", prev="impl-root"),
                 _task("fix-root", prev="review-root")):
        q.enqueue(task)
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE tasks SET status='completed' WHERE task_id IN ('impl-root','review-root')")
    for task_id in ("impl-root", "review-root"):
        capture_result_best_effort(db_path, task_id, TaskResult(
            task_id=task_id, status="completed", summary=f"result {task_id}"))

    predecessors, pr_number = task_lineage(db_path, "fix-root")
    assert predecessors == ("review-root", "impl-root")
    assert pr_number == 590
    records = storage.retrieve_shadow(
        scope=MemoryScope(project="agent_crew", task_id="fix-root"),
        layers={"decision"}, limit=10, predecessor_task_ids=predecessors,
    )[0]
    assert [record.value["task_id"] for record in records[:2]] == list(predecessors)
    assert all(record.value["pr_number"] == 590 for record in records[:2])
    assert records[0].value["predecessor_task_ids"] == ["impl-root"]
    result = RuntimeMemoryProvider(storage).retrieve(MemoryRequest(
        project="agent_crew", task_id="fix-root", predecessor_task_ids=predecessors))
    assert _lineage_recall_observation(result, predecessors) is True


def test_same_pr_predecessor_is_required_without_prev_link(tmp_path):
    db_path = str(tmp_path / "tasks.db")
    q = TaskQueue(db_path)
    q.enqueue(_task("earlier"))
    q.enqueue(_task("later"))
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE tasks SET status='completed' WHERE task_id='earlier'")
    assert task_lineage(db_path, "later") == (("earlier",), 590)


def test_keyed_lineage_read_keeps_project_boundary(tmp_path):
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    storage.put(MemoryRecord(
        "decision", "task:prior:decision", {"task_id": "prior"},
        MemoryScope(project="other-project", task_id="prior")))
    result = RuntimeMemoryProvider(storage).retrieve(MemoryRequest(
        project="agent_crew", task_id="current", predecessor_task_ids=("prior",)))
    assert result.items == ()
    assert _lineage_recall_observation(result, ("prior",)) is False


def test_shared_store_recalls_same_pr_without_local_task_row(tmp_path):
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    capture_task_outcome(storage, project="agent_crew", repo="agent_crew",
                         task_id="other-instance", status="completed",
                         summary="prior result", pr_number=590)
    capture_task_outcome(storage, project="halla", repo="halla",
                         task_id="foreign", status="completed",
                         summary="foreign result", pr_number=590)
    result = RuntimeMemoryProvider(storage).retrieve(MemoryRequest(
        project="agent_crew", task_id="current", pr_number=590))
    assert "task:other-instance:decision" in {item.item_id for item in result.items}
    assert "task:foreign:decision" not in {item.item_id for item in result.items}


def test_required_context_recalled_is_one_zero_or_null(tmp_path):
    q = TaskQueue(str(tmp_path / "tasks.db"))
    q.enqueue(_task("fix-root"))
    q.record_attribution("fix-root", project="agent_crew")
    from agent_crew.memory import MemoryItem, MemoryResult

    full = MemoryResult(provider="test", items=(
        MemoryItem("task:review-root:decision", "agent_crew", "decision", "review"),
        MemoryItem("task:impl-root:decision", "agent_crew", "decision", "implement"),
    ))
    partial = MemoryResult(provider="test", items=full.items[:1])
    for result, expected in ((full, 1), (partial, 0)):
        q.record_required_context_recalled(
            "fix-root", _lineage_recall_observation(result, ("review-root", "impl-root")))
        with sqlite3.connect(q.db_path) as db:
            assert db.execute("SELECT required_context_recalled FROM task_attribution "
                              "WHERE task_id='fix-root'").fetchone()[0] == expected
    q.record_required_context_recalled("fix-root", _lineage_recall_observation(full, ()))
    with sqlite3.connect(q.db_path) as db:
        assert db.execute("SELECT required_context_recalled FROM task_attribution "
                          "WHERE task_id='fix-root'").fetchone()[0] is None


def test_dispatch_persists_shadow_lineage_observation(tmp_path, monkeypatch,
                                                       unused_tcp_port):
    import agent_crew.server as server
    monkeypatch.setattr(server, "task_lineage", lambda _db, _task: (("prior",), None))
    provider = FakeMemoryProvider([MemoryItem(
        "task:prior:decision", "project-a", "decision", "prior-source")])
    case = tmp_path / "dispatch"
    _dispatch_snapshot(case, monkeypatch, provider, unused_tcp_port=unused_tcp_port)
    with sqlite3.connect(case / "tasks.db") as db:
        assert db.execute("SELECT required_context_recalled FROM task_attribution "
                          "WHERE task_id='shadow-322'").fetchone()[0] == 1


def test_indexed_read_only_shadow_retrieval_during_capture_write(tmp_path):
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    with sqlite3.connect(storage.path) as db:
        db.executemany("INSERT INTO adr001_memory VALUES (?,?,?,?,?,?)", [
            ("decision", f"task:older-{i}:decision", "{}",
             json.dumps({"project": "agent_crew", "task_id": f"older-{i}"}), 1, i)
            for i in range(2500)
        ])
        db.commit()
        plan = db.execute("EXPLAIN QUERY PLAN SELECT key FROM adr001_memory "
                          "WHERE COALESCE(json_extract(scope,'$.project'),'')=? "
                          "ORDER BY created DESC LIMIT 10", ("agent_crew",)).fetchall()
        assert any("idx_adr001_shadow_project_created" in row[3] for row in plan)
    writer = sqlite3.connect(storage.path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE adr001_memory SET value='{}' WHERE key='task:older-1:decision'")
        result = shadow_retrieve_bounded(RuntimeMemoryProvider(storage),
                                         MemoryRequest(project="agent_crew"), 0.05)
        assert result.state == "results"
    finally:
        writer.rollback()
        writer.close()
