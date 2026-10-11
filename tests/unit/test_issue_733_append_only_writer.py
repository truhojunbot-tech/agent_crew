"""ADR-001 value revisions preserve earlier source text (#733)."""

import json
import sqlite3

import pytest

from agent_crew.memory_hybrid import HybridMemoryStorage
from agent_crew.memory_runtime import MemoryRecord, MemoryScope, SQLiteMemoryStorage


@pytest.mark.parametrize("storage_type", [SQLiteMemoryStorage, HybridMemoryStorage])
def test_put_appends_two_revisions_without_changing_original_text(tmp_path, storage_type):
    store = storage_type(str(tmp_path / "memory.db"))
    scope = MemoryScope(project="demo")
    original = "first statement — 원문"
    for version, text in enumerate((original, "second statement", "third statement"), 1):
        store.put(MemoryRecord("procedural", "rule:1", {"text": text}, scope, version))

    with sqlite3.connect(store.path) as db:
        rows = db.execute(
            "SELECT key,value,version,superseded_by FROM adr001_memory "
            "WHERE layer='procedural' ORDER BY version"
        ).fetchall()
    assert len(rows) == 3
    assert rows[0][0] == "rule:1"
    assert json.loads(rows[0][1])["text"].encode() == original.encode()
    assert [row[3] for row in rows] == [rows[1][0], rows[2][0], None]
    assert [record.value["text"] for record in store.retrieve(scope)] == ["third statement"]


@pytest.mark.parametrize("storage_type", [SQLiteMemoryStorage, HybridMemoryStorage])
def test_shadow_recapture_appends_and_retirement_preserves_text(tmp_path, storage_type):
    store = storage_type(str(tmp_path / "memory.db"))
    scope = MemoryScope(project="demo")
    for text in ("first", "second", "third"):
        store.put_many_shadow([
            MemoryRecord("failure_pattern", "task:one:failure_pattern", {"text": text}, scope)
        ])
    with sqlite3.connect(store.path) as db:
        rows = db.execute(
            "SELECT key,value,superseded_by FROM adr001_memory "
            "WHERE layer='failure_pattern' ORDER BY version"
        ).fetchall()
    assert len(rows) == 3
    assert [json.loads(row[1])["text"] for row in rows] == ["first", "second", "third"]
    assert [row[2] for row in rows] == [rows[1][0], rows[2][0], None]
    assert [record.value["text"] for record in store.retrieve(scope)] == ["third"]

    store.put_many_shadow([
        MemoryRecord("failure_pattern", "unrelated", {"text": "safe"}, scope)
    ], retire_keys=("task:one:failure_pattern",))
    with sqlite3.connect(store.path) as db:
        assert db.execute(
            "SELECT value FROM adr001_memory WHERE key='task:one:failure_pattern'"
        ).fetchone()[0] == rows[0][1]
    assert [record.key for record in store.retrieve(scope)] == ["unrelated"]


@pytest.mark.parametrize("storage_type", [SQLiteMemoryStorage, HybridMemoryStorage])
def test_shadow_lineage_pin_returns_only_effective_revision(tmp_path, storage_type):
    store = storage_type(str(tmp_path / "memory.db"))
    scope = MemoryScope(project="demo", task_id="predecessor")
    for text in ("original", "correction", "final"):
        store.put_many_shadow([
            MemoryRecord("decision", "task:predecessor:decision", {"text": text}, scope)
        ])
    rows, _ = store.retrieve_shadow(
        MemoryScope(project="demo", task_id="successor"), {"decision"}, 10,
        predecessor_task_ids=("predecessor",))
    assert [record.value["text"] for record in rows] == ["final"]


def test_shadow_scope_refresh_keeps_older_scope_row_as_history(tmp_path):
    store = HybridMemoryStorage(str(tmp_path / "memory.db"))
    original = MemoryScope(project="demo", task_id="task-one")
    richer = MemoryScope(project="demo", task_id="task-one", issue="733")
    store.put_many_shadow([MemoryRecord("decision", "task:one:decision",
                                        {"text": "first"}, original)])
    store.put_many_shadow([MemoryRecord("decision", "task:one:decision",
                                        {"text": "second"}, richer)])
    with sqlite3.connect(store.path) as db:
        rows = db.execute("SELECT value,superseded_by FROM adr001_memory "
                          "WHERE layer='decision' ORDER BY version").fetchall()
    assert [json.loads(row[0])["text"] for row in rows] == ["first", "second"]
    assert rows[0][1] is not None and rows[1][1] is None
    assert [r.value["text"] for r in store.retrieve(richer)] == ["second"]


def test_hybrid_migration_links_two_natural_supersessions_without_rewriting_text(tmp_path):
    path = str(tmp_path / "memory.db")
    SQLiteMemoryStorage(path)
    scope = json.dumps({"project": "demo"})
    values = [
        json.dumps({"text": "원문 그대로"}, ensure_ascii=False),
        json.dumps({"text": "revision one", "supersedes": "rule:one"}),
        json.dumps({"text": "revision two", "supersedes": "rule:two"}),
    ]
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO adr001_memory(layer,key,value,scope,version,created) "
            "VALUES ('procedural',?,?,?,?,?)",
            [(key, value, scope, 1, float(index)) for index, (key, value) in enumerate(
                zip(("rule:one", "rule:two", "rule:three"), values))],
        )
    store = HybridMemoryStorage(path)
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT key,value,superseded_by FROM adr001_memory "
                          "WHERE layer='procedural' ORDER BY created").fetchall()
    assert rows[0][1] == values[0]
    assert [row[2] for row in rows] == ["rule:two", "rule:three", None]
    assert [record.key for record in store.retrieve(MemoryScope(project="demo"))] == [
        "rule:three"]
