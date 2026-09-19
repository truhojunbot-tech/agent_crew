import json
import sqlite3
import time

from agent_crew.memory_runtime import (
    MemoryRecord,
    MemoryScope,
    SQLiteMemoryStorage,
    reconstruct_context,
)


def test_unset_context_generation_inherits_into_a_specific_generation(tmp_path):
    store = SQLiteMemoryStorage(str(tmp_path / "memory.sqlite"))
    ancestor = MemoryScope(
        fleet="f", project="p", worktree="w", issue="i", task_id="t"
    )
    store.put(MemoryRecord("procedural", "rule", {}, ancestor))
    legacy_scope = {
        "fleet": "f", "project": "p", "worktree": "w", "issue": "i",
        "task_id": "t", "context_generation": 0, "provider_session": "",
    }
    with sqlite3.connect(store.path) as db:
        db.execute(
            "INSERT INTO adr001_memory VALUES (?,?,?,?,?,?)",
            ("procedural", "legacy-rule", "{}", json.dumps(legacy_scope), 1, time.time()),
        )

    assert {record.key for record in store.retrieve(
        MemoryScope(
            fleet="f", project="p", worktree="w", issue="i", task_id="t",
            context_generation=7,
        )
    )} == {"rule", "legacy-rule"}


def test_reconstruction_dedup_prefers_the_most_specific_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CREW_ADR001_MEMORY_ENABLED", "1")
    store = SQLiteMemoryStorage(str(tmp_path / "memory.sqlite"))
    broad = MemoryScope(fleet="f")
    specific = MemoryScope(fleet="f", project="p")
    store.put(MemoryRecord("procedural", "review-t", {"text": "broad"}, broad))
    store.put(MemoryRecord("procedural", "review-t", {"text": "specific"}, specific))

    context = reconstruct_context(store, '{"text":', '"broad"}', specific)

    assert context["records"] == [{
        "layer": "procedural", "key": "review-t", "value": {"text": "specific"},
        "scope": {
            "fleet": "f", "project": "p", "worktree": "", "issue": "",
            "task_id": "", "context_generation": None, "provider_session": "",
        },
        "version": 1,
    }]


def test_startup_migrates_legacy_generation_zero_and_resolves_collisions(tmp_path):
    path = tmp_path / "memory.sqlite"
    store = SQLiteMemoryStorage(str(path))
    legacy_scope = {
        "fleet": "f", "project": "p", "worktree": "", "issue": "",
        "task_id": "", "context_generation": 0, "provider_session": "",
    }
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO adr001_memory VALUES (?,?,?,?,?,?)",
            ("procedural", "rule", '{"version":"legacy"}', json.dumps(legacy_scope), 1, time.time()),
        )
    store.put(MemoryRecord(
        "procedural", "rule", {"version": "new"},
        MemoryScope(fleet="f", project="p"), version=2,
    ))

    SQLiteMemoryStorage(str(path))

    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT value,scope,version FROM adr001_memory WHERE layer='procedural' AND key='rule'"
        ).fetchall()
    assert rows == [(
        '{"version": "new"}',
        json.dumps({**legacy_scope, "context_generation": None}, sort_keys=True),
        2,
    )]


def test_specific_generation_does_not_match_a_different_or_unset_generation(tmp_path):
    store = SQLiteMemoryStorage(str(tmp_path / "memory.sqlite"))
    specific = MemoryScope(project="p", context_generation=3)
    store.put(MemoryRecord("procedural", "generation-three", {}, specific))

    assert store.retrieve(MemoryScope(project="p", context_generation=7)) == []
    assert store.retrieve(MemoryScope(project="p")) == []
