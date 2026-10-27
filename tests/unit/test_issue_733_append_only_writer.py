"""ADR-001 value revisions preserve earlier source text (#733)."""

import json
import sqlite3

import pytest

from agent_crew.memory_hybrid import HybridMemoryStorage
from agent_crew.memory_capture import capture_task_outcome
from agent_crew.memory_runtime import (
    MemoryRecord, MemoryScope, SQLiteMemoryStorage, reconstruct_context,
)
from agent_crew.server import _lineage_recall_observation


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


@pytest.mark.parametrize("storage_type", [SQLiteMemoryStorage, HybridMemoryStorage])
def test_lineage_recall_finds_decision_after_recapture(tmp_path, storage_type):
    store = storage_type(str(tmp_path / "memory.db"))
    options = {"project": "agent_crew", "repo": "truhojunbot-tech/agent_crew",
               "task_id": "prior", "status": "completed"}
    capture_task_outcome(store, **options, summary="first")
    assert _lineage_recall_observation(store, "", "agent_crew", ("prior",)) is True

    capture_task_outcome(store, **options, summary="corrected", issue="733")
    assert _lineage_recall_observation(store, "", "agent_crew", ("prior",)) is True
    assert _lineage_recall_observation(store, "", "agent_crew", ("missing",)) is False


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


def test_long_lived_shadow_reader_sees_columns_added_by_another_writer(tmp_path):
    path = str(tmp_path / "memory.db")
    reader = SQLiteMemoryStorage(path)
    writer = SQLiteMemoryStorage.existing(path)
    scope = MemoryScope(project="demo")
    for text in ("one", "two", "three"):
        writer.put_many_shadow([MemoryRecord("failure_pattern", "task:one:failure_pattern",
                                             {"text": text}, scope)])
    rows, _ = reader.retrieve_shadow(scope, {"failure_pattern"}, 10)
    assert [row.value["text"] for row in rows] == ["three"]


def test_natural_supersession_stays_retired_when_old_key_is_written_again(tmp_path):
    store = HybridMemoryStorage(str(tmp_path / "memory.db"))
    scope = MemoryScope(project="demo")
    store.put(MemoryRecord("procedural", "rule:one", {"text": "one"}, scope))
    store.put(MemoryRecord("procedural", "rule:two", {"text": "two",
                                                   "supersedes": "rule:one"}, scope))
    store.backfill()
    store.put(MemoryRecord("procedural", "rule:one", {"text": "resurrected"}, scope, 2))
    assert [(row.key, row.value["text"]) for row in store.retrieve(scope)] == [
        ("rule:two", "two")]


def test_natural_supersession_retires_latest_revision(tmp_path):
    store = HybridMemoryStorage(str(tmp_path / "memory.db"))
    scope = MemoryScope(project="demo")
    store.put(MemoryRecord("procedural", "rule:a", {"text": "one"}, scope))
    store.put(MemoryRecord("procedural", "rule:a", {"text": "two"}, scope, 2))
    store.put(MemoryRecord("procedural", "rule:b", {"text": "new",
                                                 "supersedes": "rule:a"}, scope))
    store.backfill()
    assert [(row.key, row.value["text"]) for row in store.retrieve(scope)] == [
        ("rule:b", "new")]


@pytest.mark.parametrize("storage_type", [SQLiteMemoryStorage, HybridMemoryStorage])
def test_effective_reads_use_logical_keys(tmp_path, storage_type, monkeypatch):
    store = storage_type(str(tmp_path / "memory.db"))
    scope = MemoryScope(project="demo")
    for version, text in ((1, "one"), (2, "two")):
        store.put(MemoryRecord("checkpoint", "checkpoint:one", {"text": text}, scope,
                               version))
        store.put_many_shadow([MemoryRecord("decision", "task:one:decision",
                                            {"text": text}, scope)])
    assert [row.key for row in store.retrieve(scope) if row.layer == "checkpoint"] == [
        "checkpoint:one"]
    rows, _ = store.retrieve_shadow(scope, {"decision"}, 10)
    assert [row.key for row in rows] == ["task:one:decision"]
    monkeypatch.setenv("AGENT_CREW_ADR001_MEMORY_ENABLED", "1")
    reconstructed = reconstruct_context(store, "reviewer", "one", scope)
    assert [row["key"] for row in reconstructed["records"]
            if row["layer"] == "checkpoint"] == ["checkpoint:one"]
    if isinstance(store, HybridMemoryStorage):
        ranked = store.retrieve_ranked(scope, "two", "implementer", 10, 10000)
        assert all("@v" not in row["key"] for row in ranked["head"] + ranked["middle"])


@pytest.mark.parametrize("storage_type", [SQLiteMemoryStorage, HybridMemoryStorage])
def test_retention_caps_effective_rows_and_history_separately(tmp_path, storage_type):
    store = storage_type(str(tmp_path / "memory.db"))
    scope = MemoryScope(project="demo")
    for index in range(5):
        store.put_many_shadow([MemoryRecord("decision", f"task:k{index}:decision",
                                            {"text": f"k{index}"}, scope)],
                              max_rows_per_project=6)
    for index in range(8):
        store.put_many_shadow([MemoryRecord("decision", "task:hot:decision",
                                            {"text": f"h{index}"}, scope)],
                              max_rows_per_project=6)
    effective = store.retrieve(scope)
    assert {row.key for row in effective} == {
        "task:hot:decision", *(f"task:k{index}:decision" for index in range(5))}
    assert next(row.value["text"] for row in effective
                if row.key == "task:hot:decision") == "h7"
    with sqlite3.connect(store.path) as db:
        history = db.execute("SELECT count(*) FROM adr001_memory "
                             "WHERE layer='decision' AND superseded_by IS NOT NULL").fetchone()[0]
    assert history <= 6


@pytest.mark.parametrize("storage_type", [SQLiteMemoryStorage, HybridMemoryStorage])
def test_retention_evicts_head_with_history_and_allows_later_write(tmp_path, storage_type):
    store = storage_type(str(tmp_path / "memory.db"))
    scope = MemoryScope(project="demo")

    def write(key, text):
        store.put_many_shadow([MemoryRecord("decision", f"task:{key}:decision",
                                            {"text": text}, scope)],
                              max_rows_per_project=6)

    write("x", "x0")
    write("x", "x1")
    for index in range(7):
        write(f"other{index}", f"other{index}")
    assert "task:x:decision" not in {row.key for row in store.retrieve(scope)}
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM adr001_memory "
                          "WHERE layer='decision' AND "
                          "(key='task:x:decision' OR key GLOB 'task:x:decision@v*')"
                          ).fetchone()[0] == 0
    write("x", "x2")
    write("x", "x3")
    assert [(row.key, row.value["text"]) for row in store.retrieve(scope)
            if row.key == "task:x:decision"] == [("task:x:decision", "x3")]
