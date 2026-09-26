import sqlite3

from agent_crew.memory import MemoryRequest, shadow_retrieve
from agent_crew.memory_runtime import (
    MemoryRecord, MemoryScope, SQLiteMemoryStorage,
    RuntimeMemoryProvider, ingest_blackboard_entry,
)


def test_scope_hierarchy_and_invalidated_records(tmp_path):
    store = SQLiteMemoryStorage(str(tmp_path / "memory.sqlite"))
    scopes = {
        "fleet": MemoryScope(fleet="fleet"),
        "project": MemoryScope(fleet="fleet", project="p"),
        "task": MemoryScope(fleet="fleet", project="p", task_id="t"),
        "sibling": MemoryScope(fleet="fleet", project="other"),
    }
    for key, scope in scopes.items():
        store.put(MemoryRecord("episodic", key, {"link": key}, scope))
    store.put(MemoryRecord("episodic", "invalid", {"invalidated_at": "2026-09-26"}, scopes["project"]))
    store.put(MemoryRecord("episodic", "superseded", {"superseded": True}, scopes["project"]))
    assert {r.key for r in store.retrieve(MemoryScope(fleet="fleet", project="p", issue="51", task_id="t"))} == {
        "fleet", "project", "task"
    }
    assert {r.key for r in store.retrieve(MemoryScope(fleet="fleet", project="other"))} == {
        "fleet", "sibling"
    }


def test_runtime_provider_returns_fleet_and_project_without_sibling(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_ADR001_MEMORY_ENABLED", "1")
    store = SQLiteMemoryStorage(str(tmp_path / "memory.sqlite"))
    for key, scope in (
        ("fleet", MemoryScope(fleet="f")),
        ("project", MemoryScope(fleet="f", project="p")),
        ("sibling", MemoryScope(fleet="f", project="other")),
    ):
        store.put(MemoryRecord("episodic", key, {"link": "https://example.test/" + key}, scope))
    provider = RuntimeMemoryProvider(store, fleet="f")
    result = shadow_retrieve(provider, MemoryRequest(project="p", memory_types=("episodic",)))
    assert {item.item_id for item in result.items} == {"fleet", "project"}
    assert {item.project for item in result.items} == {"", "p"}


def test_blackboard_ingest_is_idempotent(tmp_path):
    store = SQLiteMemoryStorage(str(tmp_path / "memory.sqlite"))
    entry = {"id": "board-1", "from": "p", "type": "result", "link": "https://github.com/o/r/issues/51",
             "topic": "memory", "status": "done", "result_link": "https://github.com/o/r/pull/52"}
    ingest_blackboard_entry(store, entry)
    ingest_blackboard_entry(store, entry)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM adr001_memory WHERE key='board-1'").fetchone()[0] == 1
    (record,) = store.retrieve(MemoryScope(project="p", issue="51"), exact_key="board-1")
    assert record.layer == "episodic"
    assert record.value == entry


def test_runtime_provider_is_not_used_when_shadow_flag_off(tmp_path, monkeypatch):
    from agent_crew.memory_runtime import reconstruct_context
    store = SQLiteMemoryStorage(str(tmp_path / "memory.sqlite"))
    monkeypatch.delenv("AGENT_CREW_ADR001_MEMORY_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_CREW_SHADOW_MEMORY_ENABLED", raising=False)
    assert reconstruct_context(store, "implementer", "t", MemoryScope(project="p"))["records"] == []
