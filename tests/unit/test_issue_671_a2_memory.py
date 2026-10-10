"""A2 ratification checks for the single ADR-001 store (#671)."""
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from agent_crew.memory_runtime import HybridMemoryStorage, MemoryRecord, MemoryScope, SQLiteMemoryStorage
from agent_crew.memory_hybrid import backup_daily, memory_storage_from_env


class Embedder:
    model_id = "test-model"

    def __call__(self, text):
        return [1.0, 0.0] if ("car" in text or "automobile" in text) else [0.0, 1.0]


@pytest.fixture(params=["synthetic", "live_copy"])
def store(tmp_path, request):
    path = tmp_path / "memory.db"
    if request.param == "live_copy":
        source = os.getenv("AGENT_CREW_MEMORY_DB", "")
        if not source or not os.path.isfile(source):
            pytest.skip("live ADR-001 DB path not supplied")
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as original:
            with sqlite3.connect(path) as copy:
                original.backup(copy)
    return HybridMemoryStorage(str(path), embedder=Embedder())


def put(store, key, project="alfred", *, fleet="", layer="episodic", value=None):
    store.put(MemoryRecord(layer, key, value or {"text": key},
                           MemoryScope(fleet=fleet, project=project)))


def keys(result):
    return [row["key"] for row in result["head"] + result["middle"]]


def query(store, project="alfred", *, fleet="", text="signal"):
    return store.retrieve_ranked(MemoryScope(fleet=fleet, project=project),
                                 text, "implementer", 20, 20000)


def test_r1_second_connection_supersession_is_not_served(store):
    put(store, "sd:old", layer="authoritative", value={"text": "signal", "kind": "standing_decision"})
    with sqlite3.connect(store.path) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO adr001_memory(layer,key,value,scope,version,created) "
                   "SELECT layer,'sd:new',?,scope,1,created+1 FROM adr001_memory WHERE key='sd:old'",
                   (json.dumps({"text": "signal", "kind": "standing_decision", "supersedes": "sd:old"}),))
        db.execute("UPDATE adr001_memory SET superseded_by='sd:new' WHERE key='sd:old'")
        db.commit()
    assert "sd:old" not in keys(query(store))
    assert [row["key"] for row in store.render_head("alfred")["records"] if row["key"].startswith("sd:")] == ["sd:new"]


def test_hybrid_schema_accepts_named_six_column_writers(store):
    with sqlite3.connect(store.path) as db:
        assert len(db.execute("PRAGMA table_info(adr001_memory)").fetchall()) == 8
        db.execute("INSERT INTO adr001_memory(layer,key,value,scope,version,created) VALUES (?,?,?,?,?,?)",
                   ("episodic", "legacy-writer", json.dumps({"text": "signal"}),
                    json.dumps({"project": "alfred"}), 1, 1.0))
        db.commit()
    assert "legacy-writer" in keys(query(store))


def test_legacy_json_flags_migrate_once_and_supersedes_links_same_scope(tmp_path, caplog):
    path = tmp_path / "legacy.db"
    SQLiteMemoryStorage(str(path))
    rows = [
        ("failure_pattern", "old-flag", {"text": "signal", "superseded": True}, "alfred"),
        ("failure_pattern", "old-time", {"text": "signal", "superseded_at": 43.0}, "alfred"),
        ("failure_pattern", "invalid", {"text": "signal", "invalidated_at": 42.0}, "alfred"),
        ("failure_pattern", "old-link", {"text": "signal", "superseded": "new-link"}, "alfred"),
        ("authoritative", "sd:old", {"text": "old"}, "alfred"),
        ("authoritative", "sd:new", {"text": "new", "supersedes": "sd:old"}, "alfred"),
        ("authoritative", "sd:old", {"text": "other project"}, "other"),
    ]
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO adr001_memory(layer,key,value,scope,version,created) VALUES (?,?,?,?,?,?)",
                       [(layer, key, json.dumps(value), json.dumps({"project": project}), 1, float(i))
                        for i, (layer, key, value, project) in enumerate(rows)])
        db.commit()
    with caplog.at_level("INFO"):
        store = HybridMemoryStorage(str(path))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM adr001_memory WHERE invalidated_at IS NOT NULL").fetchone()[0] == 3
        assert db.execute("SELECT invalidated_at FROM adr001_memory WHERE key='old-time'").fetchone()[0] == 43.0
        assert db.execute("SELECT superseded_by FROM adr001_memory WHERE key='old-link'").fetchone()[0] == "new-link"
        assert db.execute("SELECT superseded_by FROM adr001_memory WHERE key='sd:old' AND scope=?",
                          (json.dumps({"project": "alfred"}),)).fetchone()[0] == "sd:new"
        assert db.execute("SELECT superseded_by FROM adr001_memory WHERE key='sd:old' AND scope=?",
                          (json.dumps({"project": "other"}),)).fetchone()[0] is None
    assert "invalidated=3" in caplog.text and "superseded=2" in caplog.text
    assert "sd:old" not in keys(query(store))
    with caplog.at_level("INFO"):
        HybridMemoryStorage(str(path))
    assert "invalidated=0" in caplog.text and "superseded=0" in caplog.text


def test_hybrid_rejects_new_legacy_json_flags(store):
    put(store, "valid", value={"text": "signal"})
    with sqlite3.connect(store.path) as db:
        for flag in ("superseded_at", "superseded", "invalidated_at"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("INSERT INTO adr001_memory(layer,key,value,scope,version,created) "
                           "VALUES (?,?,?,?,?,?)",
                           ("episodic", flag, json.dumps({flag: True}), '{}', 1, 1.0))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE adr001_memory SET value=? WHERE key='valid'",
                       (json.dumps({"invalidated_at": 1}),))


def test_hybrid_shadow_retirement_writes_column(store):
    put(store, "retired", layer="failure_pattern", value={"text": "signal"})
    store.put_many_shadow([MemoryRecord(
        "failure_pattern", "fresh", {"text": "signal"}, MemoryScope(project="alfred"))],
        retire_keys=("retired",))
    with sqlite3.connect(store.path) as db:
        value, invalidated_at = db.execute(
            "SELECT value,invalidated_at FROM adr001_memory WHERE key='retired'").fetchone()
    assert invalidated_at is not None
    assert "superseded_at" not in json.loads(value)
    assert "retired" not in keys(query(store))


def test_hybrid_drop_restores_positional_writer_without_resurrection(tmp_path):
    from scripts.drop_hybrid_memory import drop_hybrid_schema

    path = tmp_path / "memory.db"
    store = HybridMemoryStorage(str(path))
    put(store, "retired", layer="failure_pattern", value={"text": "retired"})
    put(store, "replaced", layer="failure_pattern", value={"text": "replaced"})
    with sqlite3.connect(path) as db:
        db.execute("UPDATE adr001_memory SET invalidated_at=123.0 WHERE key='retired'")
        db.execute("UPDATE adr001_memory SET superseded_by='new' WHERE key='replaced'")
        db.commit()
    result = drop_hybrid_schema(path, apply=True)
    assert result["invalidated"] == 1
    assert result["superseded"] == 1
    assert result["backup"]
    with sqlite3.connect(path) as db:
        assert len(db.execute("PRAGMA table_info(adr001_memory)").fetchall()) == 6
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name IN "
                          "('adr001_fts','adr001_vec')").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' "
                          "AND name LIKE 'adr001_%'").fetchone()[0] == 0
        db.execute("INSERT INTO adr001_memory VALUES (?,?,?,?,?,?)",
                   ("failure_pattern", "legacy-writer", json.dumps({"text": "live"}),
                    json.dumps({"project": "alfred"}), 1, 1.0))
        db.commit()
    records, _ = SQLiteMemoryStorage(str(path)).retrieve_shadow(
        MemoryScope(project="alfred"), {"failure_pattern"}, 10)
    assert {row.key for row in records} == {"legacy-writer"}


def test_vector_path_ranks_without_optional_numpy(store):
    put(store, "car-memory", value={"text": "car signal"})
    result = query(store, text="automobile")
    assert result["mode"] == "hybrid"
    assert "car-memory" in keys(result)


def test_column_effectiveness_flags_and_delete_trigger(store):
    put(store, "superseded", value={"text": "signal"})
    put(store, "invalidated", value={"text": "signal"})
    put(store, "deleted", value={"text": "signal"})
    query(store)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE adr001_memory SET superseded_by='replacement' "
                   "WHERE key='superseded'")
        db.execute("UPDATE adr001_memory SET invalidated_at=123.0 "
                   "WHERE key='invalidated'")
        rowid = db.execute("SELECT rowid FROM adr001_memory WHERE key='deleted'").fetchone()[0]
        db.execute("DELETE FROM adr001_memory WHERE rowid=?", (rowid,))
        assert db.execute("SELECT count(*) FROM adr001_fts WHERE rowid=?", (rowid,)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM adr001_vec WHERE rowid=?", (rowid,)).fetchone()[0] == 0
        db.commit()
    assert not {"superseded", "invalidated", "deleted"} & set(keys(query(store)))


def test_lazy_embedding_stops_at_32_per_call(store):
    calls = []
    def embed(text):
        calls.append(text)
        return [1.0, 0.0]
    embed.model_id = "counting"
    store.embedder = embed
    store.model_id = embed.model_id
    with sqlite3.connect(store.path) as db:
        db.executemany("INSERT INTO adr001_memory(layer,key,value,scope,version,created) VALUES (?,?,?,?,?,?)", [
            ("episodic", f"cap-{i}", json.dumps({"text": f"unique-{i}"}),
             json.dumps({"project": "alfred"}), 1, float(i)) for i in range(40)
        ])
        db.commit()
    query(store, text="unmatched")
    assert len([text for text in calls if text != "unmatched"]) <= 32


def test_backend_unset_and_backup_retention(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    monkeypatch.delenv("AGENT_CREW_MEMORY_BACKEND", raising=False)
    assert type(memory_storage_from_env(str(path))) is SQLiteMemoryStorage
    with sqlite3.connect(path) as db:
        assert len(db.execute("PRAGMA table_info(adr001_memory)").fetchall()) == 6
    backup_dir = tmp_path / "backups"
    monkeypatch.setenv("AGENT_CREW_MEMORY_BACKUP_DIR", str(backup_dir))
    for day in range(1, 10):
        (backup_dir / f"memory.db.202601{day:02d}.backup").parent.mkdir(exist_ok=True)
        (backup_dir / f"memory.db.202601{day:02d}.backup").write_bytes(b"old")
    assert backup_daily(str(path), keep=7)
    assert len(list(backup_dir.glob("*.backup"))) == 7


def test_r2_no_cross_project_hit(store):
    put(store, "own", value={"text": "signal"})
    put(store, "other", project="other", value={"text": "signal"})
    assert "other" not in keys(query(store))


def test_r4_raw_sql_update_replaces_fts_and_invalidates_vector(store):
    put(store, "owner:alfred:1", project="r4-project", layer="authoritative", value={"text": "car signal"})
    query(store, project="r4-project", text="car")
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE adr001_memory SET value=? WHERE key=?",
                   (json.dumps({"text": "garden signal"}), "owner:alfred:1"))
        assert db.execute("SELECT count(*) FROM adr001_vec WHERE rowid IN "
                          "(SELECT rowid FROM adr001_memory WHERE key='owner:alfred:1')"
                          ).fetchone()[0] == 0
        db.commit()
    assert "owner:alfred:1" in keys(query(store, project="r4-project", text="garden"))


def test_r7_latency_budget_falls_back_and_counts(store, monkeypatch):
    put(store, "slow", value={"text": "signal"})
    monkeypatch.setattr(store, "_rank_middle", lambda *a, **k: __import__("time").sleep(.35))
    result = query(store)
    assert result["mode"] == "fallback"
    assert store.fallback_count == 1


def test_r9_render_head_is_stable(store):
    put(store, "owner_principle:one", layer="procedural", value={"text": "principle"})
    first = store.render_head("alfred")
    second = HybridMemoryStorage(store.path).render_head("alfred")
    assert first == second
    assert "owner_principle:one" in [r["key"] for r in first["records"]]


def test_fleet_principle_and_project_decision_share_deterministic_head(store):
    import subprocess
    import sys

    put(store, "owner_principle:fleet-rule", project="", fleet="fleet",
        layer="procedural", value={"text": "fleet rule"})
    put(store, "standing:project-rule", project="alfred", layer="decision",
        value={"kind": "standing_decision", "text": "project rule"})
    scope = MemoryScope(project="alfred", fleet="fleet")
    first = store.retrieve_ranked(scope, "rule", "implementer", 10, 4000)
    assert {"owner_principle:fleet-rule", "standing:project-rule"} <= {
        row["key"] for row in first["head"]}
    assert first["head_hash"] != hashlib.sha256(b"[]").hexdigest()
    code = ("import json,sys; from agent_crew.memory_hybrid import HybridMemoryStorage; "
            "s=HybridMemoryStorage(sys.argv[1]); "
            "print(json.dumps(s.render_head('alfred','fleet'),sort_keys=True))")
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    snapshots = [subprocess.check_output(
                     [sys.executable, "-c", code, store.path], env=env)
                 for _ in range(2)]
    assert snapshots[0] == snapshots[1]
    assert json.loads(snapshots[0])["head_hash"] == first["head_hash"]


def test_head_rejoins_effectiveness_after_a_second_writer_supersedes(store, monkeypatch):
    put(store, "owner_principle:retired", project="", fleet="fleet",
        layer="procedural", value={"text": "signal"})
    original_rank = store._rank_middle

    def retire_during_rank(*args):
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE adr001_memory SET superseded_by='replacement' "
                       "WHERE key='owner_principle:retired'")
            db.commit()
        return original_rank(*args)

    monkeypatch.setattr(store, "_rank_middle", retire_during_rank)
    result = query(store, fleet="fleet")
    assert "owner_principle:retired" not in keys(result)
    assert result["superseded_served"] == 0
    assert result["head_bytes"] == len(json.dumps(result["head"], sort_keys=True,
                                                  ensure_ascii=False).encode())


def test_r27_fleet_and_project_ancestor_in_one_list(store):
    put(store, "owner:alfred:one", project="r27-project", layer="authoritative", value={"text": "r27signal"})
    put(store, "owner_principle:fleet", project="", fleet="fleet",
        layer="procedural", value={"text": "r27signal"})
    assert {"owner:alfred:one", "owner_principle:fleet"} <= set(
        keys(query(store, project="r27-project", fleet="fleet", text="r27signal")))


def test_ranked_http_endpoint_uses_hybrid_store(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from agent_crew.server import create_app

    path = tmp_path / "memory.db"
    memory = HybridMemoryStorage(str(path), embedder=Embedder())
    put(memory, "http-signal", project="demo", value={"text": "http-signal"})
    monkeypatch.setenv("AGENT_CREW_MEMORY_BACKEND", "hybrid")
    monkeypatch.setenv("AGENT_CREW_MEMORY_DB", str(path))
    with TestClient(create_app(str(tmp_path / "tasks.db"), project="demo",
                               watchdog_disabled=True)) as client:
        response = client.post("/memory/retrieve_ranked", headers={"X-Agent-Crew-Project": "demo"},
                               json={"scope": {"project": "demo"}, "query": "http-signal",
                                     "role": "implementer", "k": 10, "byte_budget": 4000})
    assert response.status_code == 200
    assert "http-signal" in keys(response.json())


def test_ranked_http_timeout_does_not_block_event_loop_or_double_count(tmp_path, monkeypatch):
    import threading
    import time
    from fastapi.testclient import TestClient
    from agent_crew.server import create_app

    path = tmp_path / "memory.db"
    SQLiteMemoryStorage(str(path)).put(MemoryRecord(
        "episodic", "slow", {"text": "signal"}, MemoryScope(project="demo")))
    monkeypatch.setenv("AGENT_CREW_MEMORY_BACKEND", "hybrid")
    monkeypatch.setenv("AGENT_CREW_MEMORY_DB", str(path))
    instances = []
    original_init = HybridMemoryStorage.__init__
    original_render = HybridMemoryStorage.render_head
    original_rank = HybridMemoryStorage._rank_middle
    main_thread = threading.current_thread()

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        instances.append(self)

    def render(self, project, fleet=""):
        assert threading.current_thread() is not main_thread
        return original_render(self, project, fleet)

    def slow_rank(self, *args):
        time.sleep(.45)
        return original_rank(self, *args)

    monkeypatch.setattr(HybridMemoryStorage, "__init__", init)
    monkeypatch.setattr(HybridMemoryStorage, "render_head", render)
    monkeypatch.setattr(HybridMemoryStorage, "_rank_middle", slow_rank)
    monkeypatch.setattr(HybridMemoryStorage, "missing_vector_count",
                        lambda self: pytest.fail("timeout path scanned vector table"))
    with TestClient(create_app(str(tmp_path / "tasks.db"), project="demo",
                               watchdog_disabled=True)) as client:
        response = client.post("/memory/retrieve_ranked", headers={"X-Agent-Crew-Project": "demo"},
                               json={"scope": {"project": "demo"}, "query": "signal",
                                     "role": "implementer", "k": 10, "byte_budget": 4000})
        assert response.status_code == 200
        assert response.json()["mode"] == "fallback"
        time.sleep(.25)  # The worker finishes after the HTTP timeout.
    assert instances[-1].fallback_count == 1
