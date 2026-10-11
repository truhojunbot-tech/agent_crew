"""#742: migrate the existing ADR-001 index for Korean substring search."""
import json
import sqlite3

from agent_crew.memory_hybrid import HybridMemoryStorage, ensure_index_schema
from agent_crew.memory_runtime import MemoryScope, SQLiteMemoryStorage


def test_old_fts_rebuild_finds_korean_substring_and_keeps_short_and_id_queries(tmp_path):
    path = tmp_path / "memory.db"
    SQLiteMemoryStorage(str(path))
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO adr001_memory(layer,key,value,scope,version,created) "
            "VALUES (?,?,?,?,?,?)",
            [("episodic", "owner:quota:abc-2154", json.dumps({"text": "토큰노믹스 게이트 측정"}),
              json.dumps({"project": "agent_crew"}), 1, 1.0),
             ("episodic", "owner:english:abc-2154", json.dumps({"text": "review gate"}),
              json.dumps({"project": "agent_crew"}), 1, 2.0)],
        )
        db.execute("CREATE VIRTUAL TABLE adr001_fts USING fts5(layer,key,body)")
        db.execute("INSERT INTO adr001_fts(rowid,layer,key,body) "
                   "SELECT rowid,layer,key,value FROM adr001_memory")
        db.commit()
        assert not db.execute("SELECT key FROM adr001_fts WHERE adr001_fts MATCH '노믹스'").fetchall()
        ensure_index_schema(db)
        db.commit()
        definition = db.execute("SELECT sql FROM sqlite_master WHERE name='adr001_fts'").fetchone()[0]
        assert "trigram" in definition
        assert {row[0] for row in db.execute(
            "SELECT key FROM adr001_fts WHERE adr001_fts MATCH '노믹스'")} == {"owner:quota:abc-2154"}
        assert "owner:english:abc-2154" in {row[0] for row in db.execute(
            "SELECT key FROM adr001_fts WHERE adr001_fts MATCH 'english'")}
        store = HybridMemoryStorage(str(path))
        scope = MemoryScope(project="agent_crew")
        assert "owner:quota:abc-2154" in {row["key"] for row in store._candidates(db, scope, "측정")}
        assert "owner:english:abc-2154" in {row["key"] for row in store._candidates(db, scope, "abc-2154")}
        ensure_index_schema(db)
        assert db.execute("SELECT count(*) FROM adr001_fts").fetchone()[0] == 2


def test_fts_definition_rebuild_rolls_back_as_one_unit(tmp_path):
    path = tmp_path / "memory.db"
    SQLiteMemoryStorage(str(path))
    with sqlite3.connect(path) as db:
        db.execute("CREATE VIRTUAL TABLE adr001_fts USING fts5(layer,key,body)")
        db.execute("INSERT INTO adr001_memory(layer,key,value,scope,version,created) "
                   "VALUES ('episodic','owner:korean',?,?,1,1)",
                   (json.dumps({"text": "토큰노믹스 게이트 측정"}), json.dumps({"project": "agent_crew"})))
        db.commit()
        ensure_index_schema(db)
        db.rollback()
        assert "trigram" not in db.execute(
            "SELECT sql FROM sqlite_master WHERE name='adr001_fts'").fetchone()[0]
        ensure_index_schema(db)
        db.commit()
        assert db.execute("SELECT key FROM adr001_fts WHERE adr001_fts MATCH '노믹스'").fetchone()[0] == "owner:korean"
