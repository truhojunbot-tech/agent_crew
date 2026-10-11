"""#742: migrate the existing ADR-001 index for Korean substring search."""
import json
import sqlite3

from agent_crew.memory_hybrid import HybridMemoryStorage, ensure_index_schema
from agent_crew.memory_runtime import MemoryScope, SQLiteMemoryStorage


def _seed_with_newer_noise(path, targets, noise=110, noise_body="unrelated",
                           noise_layer="episodic"):
    SQLiteMemoryStorage(str(path))
    with sqlite3.connect(path) as db:
        rows = [("episodic", key, json.dumps({"text": body}),
                 json.dumps({"project": "agent_crew"}), 1, float(index))
                for index, (key, body) in enumerate(targets, 1)]
        rows += [(noise_layer, f"noise:{index:03}", json.dumps({"text": noise_body}),
                  json.dumps({"project": "agent_crew"}), 1, float(index + 100))
                 for index in range(noise)]
        db.executemany(
            "INSERT INTO adr001_memory(layer,key,value,scope,version,created) "
            "VALUES (?,?,?,?,?,?)", rows)
        db.commit()


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
        ensure_index_schema(db)
        assert db.execute("SELECT count(*) FROM adr001_fts").fetchone()[0] == 2
        # Recent-row padding alone must not make these assertions pass.
        db.executemany(
            "INSERT INTO adr001_memory(layer,key,value,scope,version,created) "
            "VALUES (?,?,?,?,?,?)",
            [("episodic", f"noise:{index:03}", json.dumps({"text": "unrelated"}),
              json.dumps({"project": "agent_crew"}), 1, float(index + 100))
             for index in range(110)],
        )
        db.commit()
        scope = MemoryScope(project="agent_crew")
        assert "owner:quota:abc-2154" in {row["key"] for row in store._candidates(db, scope, "측정")}
        assert "owner:english:abc-2154" in {row["key"] for row in store._candidates(db, scope, "abc-2154")}


def test_mixed_query_keeps_short_korean_terms_in_candidates(tmp_path):
    path = tmp_path / "memory.db"
    _seed_with_newer_noise(path, [("owner:short", "토큰 한도 보호가 우선")])
    store = HybridMemoryStorage(str(path))
    with sqlite3.connect(path) as db:
        keys = [row["key"] for row in store._candidates(
            db, MemoryScope(project="agent_crew"), "토큰 한도 claude")]
    assert "owner:short" in keys


def test_mixed_query_reserves_long_term_lane_when_short_matches_fill_limit(tmp_path):
    path = tmp_path / "memory.db"
    _seed_with_newer_noise(path, [("owner:long", "claude pipeline")],
                           noise_body="토큰")
    store = HybridMemoryStorage(str(path))
    with sqlite3.connect(path) as db:
        keys = [row["key"] for row in store._candidates(
            db, MemoryScope(project="agent_crew"), "토큰 claude")]
    assert "owner:long" in keys


def test_short_queries_match_case_insensitive_body_and_key_before_recent_padding(tmp_path):
    path = tmp_path / "memory.db"
    _seed_with_newer_noise(path, [("owner:body", "PR 751 merged after CI green"),
                                  ("PR-751:owner", "merged and green")],
                           noise_layer="decision")
    store = HybridMemoryStorage(str(path))
    with sqlite3.connect(path) as db:
        scope = MemoryScope(project="agent_crew")
        ci_keys = [row["key"] for row in store._candidates(db, scope, "ci")]
        pr_keys = [row["key"] for row in store._candidates(db, scope, "pr")]
    assert "owner:body" in ci_keys
    assert "PR-751:owner" in pr_keys


def test_short_terms_ignore_colliding_layer_names(tmp_path):
    for term, layer, body, noise_body in (
            ("ci", "decision", "CI gate passed", "decision unrelated"),
            ("pr", "procedural", "PR 751 merged", "protect unrelated"),
            ("is", "episodic", "it is done", "episodic unrelated")):
        path = tmp_path / f"{term}.db"
        _seed_with_newer_noise(path, [("owner:target", body)],
                               noise_layer=layer, noise_body=noise_body)
        store = HybridMemoryStorage(str(path))
        with sqlite3.connect(path) as db:
            keys = [row["key"] for row in store._candidates(
                db, MemoryScope(project="agent_crew"), term)]
        assert "owner:target" in keys, (term, layer)
        assert keys[0] == "owner:target", (term, layer)


def test_mixed_query_bm25_hit_precedes_substring_only_short_match(tmp_path):
    path = tmp_path / "memory.db"
    _seed_with_newer_noise(
        path, [("owner:target", "Review 751 trigram index")], noise=1,
        noise_body="protect unrelated", noise_layer="procedural")
    store = HybridMemoryStorage(str(path))
    with sqlite3.connect(path) as db:
        candidates = store._candidates(
            db, MemoryScope(project="agent_crew"), "Review PR 751 trigram index")
    ranked, _ = store._rank_middle(candidates, "Review PR 751 trigram index")
    assert ranked[0]["key"] == "owner:target"


def test_short_only_terms_rank_lexical_hit_ahead_of_recent_noise(tmp_path):
    path = tmp_path / "memory.db"
    _seed_with_newer_noise(path, [("owner:short", "PR 751 merged after CI green")], noise=1)
    store = HybridMemoryStorage(str(path))
    with sqlite3.connect(path) as db:
        candidates = store._candidates(db, MemoryScope(project="agent_crew"))
    ranked, _ = store._rank_middle(candidates, "ci")
    assert ranked[0]["key"] == "owner:short"


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
