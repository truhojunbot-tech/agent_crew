"""A2 ratification checks for the single ADR-001 store (#671)."""
import json
import os
import sqlite3

import pytest

from agent_crew.memory_runtime import HybridMemoryStorage, MemoryRecord, MemoryScope


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
    put(store, "old", value={"text": "signal"})
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE adr001_memory SET superseded_by='new' WHERE key='old'")
        db.commit()
    assert "old" not in keys(query(store))


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
