"""#742: migrate the existing ADR-001 index for Korean substring search."""
import json
import sqlite3

import pytest

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


def test_two_character_korean_substring_inside_long_word_survives(tmp_path):
    path = tmp_path / "memory.db"
    _seed_with_newer_noise(path, [("owner:embedded", "토큰노믹스 게이트")])
    store = HybridMemoryStorage(str(path))
    with sqlite3.connect(path) as db:
        keys = [row["key"] for row in store._candidates(
            db, MemoryScope(project="agent_crew"), "노믹")]
    assert "owner:embedded" in keys


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


def test_common_short_word_cannot_evict_rare_short_hit(tmp_path):
    for query, noise_body in (("is PR", "this is a note about nothing"),
                              ("a PR", "a note")):
        path = tmp_path / f"{query.replace(' ', '-')}.db"
        _seed_with_newer_noise(path, [("owner:target", "PR 751 merged after CI green")],
                               noise_body=noise_body)
        store = HybridMemoryStorage(str(path))
        with sqlite3.connect(path) as db:
            candidates = store._candidates(db, MemoryScope(project="agent_crew"), query)
        assert candidates[0]["key"] == "owner:target", query
        ranked, _ = store._rank_middle(candidates, query)
        assert ranked[0]["key"] == "owner:target", query


def test_two_selective_short_terms_beat_one_newer_match(tmp_path):
    path = tmp_path / "memory.db"
    _seed_with_newer_noise(path, [("owner:target", "PR merged after CI green")],
                           noise_body="CI check unrelated")
    store = HybridMemoryStorage(str(path))
    with sqlite3.connect(path) as db:
        candidates = store._candidates(db, MemoryScope(project="agent_crew"), "CI PR")
    assert candidates[0]["key"] == "owner:target"


@pytest.mark.parametrize("query,noise", [
    ("no PR", "no note here"), ("my PR", "my note"),
    ("PR 배포", "배포 메모"),
])
def test_rare_short_term_beats_unlisted_common_noise(tmp_path, query, noise):
    path = tmp_path / "memory.db"
    _seed_with_newer_noise(path, [("owner:target", "PR 751 merged after CI green")],
                           noise_body=noise)
    store = HybridMemoryStorage(str(path))
    with sqlite3.connect(path) as db:
        candidates = store._candidates(db, MemoryScope(project="agent_crew"), query)
    assert candidates[0]["key"] == "owner:target"


@pytest.mark.parametrize("term,target,noise", [
    ("PR", "fix landed in PR.", "protect unrelated"),
    ("PR", "merged (PR) today", "protect unrelated"),
    ("PR", "see PR/751 for detail", "protect unrelated"),
    ("CI", "green on CI, shipped", "decision unrelated"),
])
def test_punctuation_delimits_short_ascii_terms(tmp_path, term, target, noise):
    path = tmp_path / "memory.db"
    _seed_with_newer_noise(path, [("owner:target", target)], noise_body=noise)
    store = HybridMemoryStorage(str(path))
    with sqlite3.connect(path) as db:
        candidates = store._candidates(db, MemoryScope(project="agent_crew"), term)
    assert candidates[0]["key"] == "owner:target"


@pytest.mark.parametrize("query,target", [
    ("v2 PR CI QA DB UI", "UI polish shipped"),
    ("토큰 한도 보호 우선 측정 배포", "배포 완료"),
])
def test_later_short_term_is_searched(tmp_path, query, target):
    path = tmp_path / "memory.db"
    _seed_with_newer_noise(path, [("owner:target", target)])
    store = HybridMemoryStorage(str(path))
    with sqlite3.connect(path) as db:
        candidates = store._candidates(db, MemoryScope(project="agent_crew"), query)
    assert candidates[0]["key"] == "owner:target"


@pytest.mark.parametrize("query", ["토큰 한도", "PR CI"])
def test_short_term_retrieval_stays_within_budget_at_20k_rows(tmp_path, query):
    path = tmp_path / "memory.db"
    store = HybridMemoryStorage(str(path))
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO adr001_memory(layer,key,value,scope,version,created) "
            "VALUES (?,?,?,?,?,?)",
            ("decision", "owner:old-target", json.dumps({"text": "토큰 한도: PR CI checks passed"}),
             '{"project":"agent_crew"}', 1, -1.0),
        )
        db.executemany(
            "INSERT INTO adr001_memory(layer,key,value,scope,version,created) "
            "VALUES (?,?,?,?,?,?)",
            (("decision", f"noise:{index:05}", json.dumps({"text":
              "This is a realistic task outcome with a longer description of work, "
              "review state, implementation detail, and verification evidence. " * 5}),
              '{"project":"agent_crew"}', 1, float(index))
             for index in range(20_000)),
        )
        db.commit()
    scope = MemoryScope(project="agent_crew")
    results = [store.retrieve_ranked(scope, query, "implementer", 10, 16000)
               for _ in range(3)]
    assert all(result["mode"] == "lexical_only" for result in results), results
    assert all("owner:old-target" in {row["key"] for row in result["middle"]}
               for result in results), results
    assert max(result["latency_ms"] for result in results) < 300, results


def test_short_token_index_tracks_value_updates_and_deletes(tmp_path):
    path = tmp_path / "memory.db"
    store = HybridMemoryStorage(str(path))
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO adr001_memory(layer,key,value,scope,version,created) "
                   "VALUES (?,?,?,?,?,?)", ("decision", "owner:updated",
                   json.dumps({"text": "PR check"}), '{"project":"agent_crew"}', 1, 1.0))
        db.commit()
        assert db.execute("SELECT key FROM adr001_short_fts WHERE adr001_short_fts MATCH 'PR'").fetchone()[0] == "owner:updated"
        db.execute("UPDATE adr001_memory SET value=? WHERE key='owner:updated'",
                   (json.dumps({"text": "CI check"}),))
        db.commit()
        assert not db.execute("SELECT 1 FROM adr001_short_fts WHERE adr001_short_fts MATCH 'PR'").fetchone()
        assert db.execute("SELECT key FROM adr001_short_fts WHERE adr001_short_fts MATCH 'CI'").fetchone()[0] == "owner:updated"
        db.execute("DELETE FROM adr001_memory WHERE key='owner:updated'")
        db.commit()
        assert not db.execute("SELECT 1 FROM adr001_short_fts WHERE key='owner:updated'").fetchone()


def test_short_only_terms_rank_lexical_hit_ahead_of_recent_noise(tmp_path):
    path = tmp_path / "memory.db"
    _seed_with_newer_noise(path, [("owner:short", "PR 751 merged after CI green")], noise=1)
    store = HybridMemoryStorage(str(path))
    with sqlite3.connect(path) as db:
        candidates = store._candidates(db, MemoryScope(project="agent_crew"))
    ranked, _ = store._rank_middle(candidates, "ci")
    assert ranked[0]["key"] == "owner:short"


def test_fts_definition_swap_rolls_back_as_one_unit(tmp_path):
    path = tmp_path / "memory.db"
    SQLiteMemoryStorage(str(path))
    class FailSwap(sqlite3.Connection):
        fail_swap = True

        def execute(self, sql, *args):
            if self.fail_swap and sql.startswith("ALTER TABLE adr001_fts_new RENAME"):
                raise sqlite3.OperationalError("injected swap failure")
            return super().execute(sql, *args)

    with sqlite3.connect(path, factory=FailSwap) as db:
        db.execute("CREATE VIRTUAL TABLE adr001_fts USING fts5(layer,key,body)")
        db.execute("INSERT INTO adr001_memory(layer,key,value,scope,version,created) "
                   "VALUES ('episodic','owner:korean',?,?,1,1)",
                   (json.dumps({"text": "토큰노믹스 게이트 측정"}), json.dumps({"project": "agent_crew"})))
        db.commit()
        with pytest.raises(sqlite3.OperationalError, match="injected swap failure"):
            ensure_index_schema(db)
        db.rollback()
        assert "trigram" not in db.execute(
            "SELECT sql FROM sqlite_master WHERE name='adr001_fts'").fetchone()[0]
        db.fail_swap = False
        ensure_index_schema(db)
        db.commit()
        assert db.execute("SELECT key FROM adr001_fts WHERE adr001_fts MATCH '노믹스'").fetchone()[0] == "owner:korean"


@pytest.mark.parametrize("stage", ["adr001_short_fts_new", "adr001_fts_new"])
def test_rebuild_mirrors_concurrent_write_before_swap(tmp_path, stage):
    path = tmp_path / "memory.db"
    SQLiteMemoryStorage(str(path))

    class WriteDuringStage(sqlite3.Connection):
        wrote = False

        def commit(self):
            super().commit()
            if (not self.wrote and self.execute(
                    "SELECT 1 FROM sqlite_master WHERE name=?", (stage,)).fetchone()):
                self.wrote = True
                with sqlite3.connect(path) as other:
                    other.execute("INSERT INTO adr001_memory(layer,key,value,scope,version,created) "
                                  "VALUES ('episodic','owner:concurrent',?,?,1,2)",
                                  (json.dumps({"text": "동시 기록확인"}),
                                   json.dumps({"project": "agent_crew"})))

    with sqlite3.connect(path, factory=WriteDuringStage) as db:
        db.execute("CREATE VIRTUAL TABLE adr001_fts USING fts5(layer,key,body)")
        db.execute("INSERT INTO adr001_memory(layer,key,value,scope,version,created) "
                   "VALUES ('episodic','owner:old',?,?,1,1)",
                   (json.dumps({"text": "기존 기록확인"}), json.dumps({"project": "agent_crew"})))
        db.commit()
        ensure_index_schema(db)
        assert db.wrote
        assert db.execute("SELECT key FROM adr001_fts WHERE adr001_fts MATCH '기록확인'").fetchall()
        assert {row[0] for row in db.execute(
            "SELECT key FROM adr001_fts WHERE adr001_fts MATCH '기록확인'")} == {
                "owner:old", "owner:concurrent"}
        assert db.execute("SELECT count(*) FROM adr001_short_fts").fetchone()[0] == 2


def test_rebuild_finishes_retiring_old_table_after_restart(tmp_path):
    path = tmp_path / "memory.db"
    HybridMemoryStorage(str(path))
    with sqlite3.connect(path) as db:
        db.execute("CREATE VIRTUAL TABLE adr001_fts_old USING fts5(layer,key,body)")
        db.execute("INSERT INTO adr001_fts_old(rowid,layer,key,body) "
                   "VALUES (1,'episodic','stale','stale')")
        db.commit()
        ensure_index_schema(db)
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='adr001_fts_old'").fetchone() is None
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='adr001_fts'").fetchone()
