"""English gloss terms belong in authoritative FTS bodies only (#700)."""
import json
import sqlite3

from agent_crew.memory_hybrid import ensure_index_schema
from agent_crew.memory_runtime import HybridMemoryStorage, MemoryRecord, MemoryScope


def _fts_keys(db_path, term):
    with sqlite3.connect(db_path) as db:
        return {row[0] for row in db.execute(
            "SELECT key FROM adr001_fts WHERE adr001_fts MATCH ?", (term,))}


def _install_old_triggers(db):
    db.execute("DROP TRIGGER adr001_fts_ai")
    db.execute("DROP TRIGGER adr001_fts_au")
    db.executescript("""
      CREATE TRIGGER adr001_fts_ai AFTER INSERT ON adr001_memory BEGIN
        INSERT INTO adr001_fts(rowid,layer,key,body)
        VALUES(new.rowid,new.layer,new.key,
          CASE WHEN new.layer='authoritative' THEN
            COALESCE(json_extract(new.value,'$.text'),new.value)
          ELSE new.value END);
      END;
      CREATE TRIGGER adr001_fts_au AFTER UPDATE OF value ON adr001_memory BEGIN
        DELETE FROM adr001_fts WHERE rowid=old.rowid;
        INSERT INTO adr001_fts(rowid,layer,key,body)
        VALUES(new.rowid,new.layer,new.key,
          CASE WHEN new.layer='authoritative' THEN
            COALESCE(json_extract(new.value,'$.text'),new.value)
          ELSE new.value END);
      END;
    """)


def test_authoritative_english_gloss_is_fts_searchable(tmp_path):
    store = HybridMemoryStorage(str(tmp_path / "memory.db"))
    store.put(MemoryRecord("authoritative", "owner:korean",
                           {"text": "반드시 검토", "gloss_en": "review obligations"},
                           MemoryScope(project="alfred")))
    assert "owner:korean" in _fts_keys(store.path, "obligations")


def test_missing_gloss_keeps_existing_authoritative_search(tmp_path):
    store = HybridMemoryStorage(str(tmp_path / "memory.db"))
    store.put(MemoryRecord("authoritative", "owner:plain", {"text": "기존 규칙"},
                           MemoryScope(project="alfred")))
    assert "owner:plain" in _fts_keys(store.path, "규칙")
    assert "owner:plain" not in _fts_keys(store.path, "obligations")


def test_old_trigger_migration_rebuilds_once(tmp_path):
    store = HybridMemoryStorage(str(tmp_path / "memory.db"))
    store.put(MemoryRecord("authoritative", "owner:old",
                           {"text": "기존 규칙", "gloss_en": "durable obligation"},
                           MemoryScope(project="alfred")))
    with sqlite3.connect(store.path) as db:
        _install_old_triggers(db)
        db.execute("UPDATE adr001_memory SET value=value WHERE key='owner:old'")
        db.commit()
        assert "owner:old" not in _fts_keys(store.path, "obligation")
        statements = []
        db.set_trace_callback(statements.append)
        ensure_index_schema(db)
        db.commit()
        assert "owner:old" in _fts_keys(store.path, "obligation")
        ensure_index_schema(db)
        db.commit()
        db.set_trace_callback(None)
    rebuilds = [statement for statement in statements if
                statement.strip().startswith("INSERT INTO adr001_fts(rowid,layer,key,body) SELECT")]
    assert len(rebuilds) == 1


def test_interrupted_trigger_migration_retries_rebuild(tmp_path):
    store = HybridMemoryStorage(str(tmp_path / "memory.db"))
    store.put(MemoryRecord("authoritative", "owner:old",
                           {"text": "기존 규칙", "gloss_en": "durable obligation"},
                           MemoryScope(project="alfred")))
    with sqlite3.connect(store.path) as db:
        _install_old_triggers(db)
        db.execute("UPDATE adr001_memory SET value=value WHERE key='owner:old'")
        db.commit()
        assert "owner:old" not in _fts_keys(store.path, "obligation")
        ensure_index_schema(db)
        db.rollback()  # A failed backfill or process death before commit.
        assert "owner:old" not in _fts_keys(store.path, "obligation")
        ensure_index_schema(db)
        db.commit()
    assert "owner:old" in _fts_keys(store.path, "obligation")


def test_update_trigger_refreshes_gloss_without_touching_other_layers(tmp_path):
    store = HybridMemoryStorage(str(tmp_path / "memory.db"))
    store.put(MemoryRecord("authoritative", "owner:updated",
                           {"text": "검토", "gloss_en": "earlier"},
                           MemoryScope(project="alfred")))
    store.put(MemoryRecord("episodic", "episode:unchanged",
                           {"text": "검토", "gloss_en": "episodicgloss"},
                           MemoryScope(project="alfred")))
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE adr001_memory SET value=? WHERE key='owner:updated'",
                   (json.dumps({"text": "검토", "gloss_en": "later"}),))
        db.commit()
    assert "owner:updated" in _fts_keys(store.path, "later")
    assert "owner:updated" not in _fts_keys(store.path, "earlier")
    assert "episode:unchanged" in _fts_keys(store.path, "episodicgloss")
