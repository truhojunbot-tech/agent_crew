"""#743: scoped vector and FTS arms with configurable fusion."""
import builtins
import hashlib
import json
import sqlite3
import time
from array import array

from fastapi.testclient import TestClient

from agent_crew.memory_hybrid import HybridMemoryStorage, MIDDLE_CANDIDATE_LIMIT
from agent_crew.memory_runtime import MemoryRecord, MemoryScope, SQLiteMemoryStorage
from agent_crew.memory import NullMemoryProvider
from agent_crew.server import create_app
from tests.unit.test_issue_322_memory_provider import _dispatch_snapshot


class Embedder:
    model_id = "fixture-v1"

    def __call__(self, text):
        return [1.0, 0.0] if text == "needle" or "target" in text else [0.0, 1.0]


def test_vector_only_hits_survive_union_and_foreign_scope_never_enters_arms(tmp_path):
    path = tmp_path / "memory.db"
    store = HybridMemoryStorage(str(path), embedder=Embedder())
    with sqlite3.connect(path) as db:
        rows = [("episodic", f"target:{i}", json.dumps({"text": f"target {i}"}),
                 json.dumps({"project": "home"}), 1, float(i)) for i in range(2)]
        rows.append(("episodic", "foreign:target", json.dumps({"text": "target foreign"}),
                     json.dumps({"project": "other"}), 1, 3.0))
        rows.append(("episodic", "foreign:fts", json.dumps({"text": "needle foreign"}),
                     json.dumps({"project": "other"}), 1, 4.0))
        rows.extend(("episodic", f"filler:{i}", json.dumps({"text": f"needle filler {i}"}),
                     json.dumps({"project": "home"}), 1, float(i + 10)) for i in range(110))
        db.executemany("INSERT INTO adr001_memory(layer,key,value,scope,version,created) "
                       "VALUES (?,?,?,?,?,?)", rows)
        for key in ("target:0", "target:1", "foreign:target"):
            rowid, body = db.execute("SELECT rowid,body FROM adr001_fts WHERE key=?", (key,)).fetchone()
            db.execute("INSERT INTO adr001_vec(rowid,model_id,content_sha,vec) VALUES (?,?,?,?)",
                       (rowid, Embedder.model_id, hashlib.sha256(body.encode()).hexdigest(),
                        array("f", [1.0, 0.0]).tobytes()))
        db.commit()
    scope = MemoryScope(project="home")
    default = store.retrieve_ranked(scope, "needle", "implementer", 2, 10000)
    assert not {"target:0", "target:1"} & {row["key"] for row in default["middle"]}
    assert default["fusion_mode"] == "rrf"

    union = HybridMemoryStorage(str(path), embedder=Embedder(), fusion_mode="union_vec_first")
    result = union.retrieve_ranked(scope, "needle", "implementer", 2, 10000)
    assert {row["key"] for row in result["middle"]} == {"target:0", "target:1"}
    assert result["fusion_mode"] == "union_vec_first"
    weighted = HybridMemoryStorage(str(path), embedder=Embedder(), fusion_mode="weighted",
                                   fusion_weight=1.0)
    weighted_result = weighted.retrieve_ranked(scope, "needle", "implementer", 2, 10000)
    assert {row["key"] for row in weighted_result["middle"]} == {"target:0", "target:1"}
    assert weighted_result["fusion_mode"] == "weighted"
    with sqlite3.connect(path) as db:
        candidates = union._candidates(db, scope, "needle")
    assert {"foreign:target", "foreign:fts"}.isdisjoint(
        {row["key"] for row in candidates})


def test_weighted_mode_is_configurable_and_default_rrf_is_stable(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    monkeypatch.delenv("AGENT_CREW_MEMORY_FUSION_MODE", raising=False)
    baseline = HybridMemoryStorage(str(path), embedder=Embedder())
    assert baseline.fusion_mode == "rrf"
    monkeypatch.setenv("AGENT_CREW_MEMORY_FUSION_MODE", "weighted")
    monkeypatch.setenv("AGENT_CREW_MEMORY_FUSION_WEIGHT", "0.8")
    weighted = HybridMemoryStorage(str(path), embedder=Embedder())
    assert weighted.fusion_mode == "weighted"
    assert weighted.fusion_weight == 0.8


def test_invalid_fusion_settings_are_refused(tmp_path):
    path = tmp_path / "memory.db"
    try:
        HybridMemoryStorage(str(path), embedder=Embedder(), fusion_mode="unknown")
    except ValueError as exc:
        assert "fusion mode" in str(exc)
    else:
        raise AssertionError("unknown fusion mode was accepted")
    try:
        HybridMemoryStorage(str(path), embedder=Embedder(), fusion_mode="weighted",
                            fusion_weight=1.1)
    except ValueError as exc:
        assert "fusion weight" in str(exc)
    else:
        raise AssertionError("out-of-range fusion weight was accepted")


def test_weighted_mode_without_numpy_uses_startup_fallback(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    SQLiteMemoryStorage(str(path))
    monkeypatch.setenv("AGENT_CREW_MEMORY_BACKEND", "hybrid")
    monkeypatch.setenv("AGENT_CREW_MEMORY_DB", str(path))
    monkeypatch.setenv("AGENT_CREW_MEMORY_FUSION_MODE", "weighted")
    original_import = builtins.__import__

    def missing_numpy(name, *args, **kwargs):
        if name == "numpy":
            raise ModuleNotFoundError("No module named 'numpy'")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_numpy)
    with TestClient(create_app(str(tmp_path / "tasks.db"), project="home",
                               watchdog_disabled=True, anomaly_disabled=True)) as client:
        response = client.get("/health", headers={"X-Agent-Crew-Project": "home"})
    assert response.status_code == 200
    assert response.json()["hybrid_memory"] == {"available": False,
                                                 "startup_fallback_count": 1}


def test_vector_arm_is_bounded_above_candidate_cap(tmp_path):
    path = tmp_path / "memory.db"
    store = HybridMemoryStorage(str(path), embedder=Embedder(),
                                fusion_mode="union_vec_first")
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO adr001_memory(layer,key,value,scope,version,created) "
            "VALUES (?,?,?,?,?,?)",
            [("episodic", f"vec:{i}", json.dumps({"text": f"vector row {i}"}),
              json.dumps({"project": "home"}), 1, float(i)) for i in range(400)],
        )
        rows = db.execute("SELECT rowid,body FROM adr001_fts").fetchall()
        db.executemany(
            "INSERT INTO adr001_vec(rowid,model_id,content_sha,vec) VALUES (?,?,?,?)",
            [(rowid, Embedder.model_id, hashlib.sha256(body.encode()).hexdigest(),
              array("f", [1.0, 0.0]).tobytes()) for rowid, body in rows],
        )
        db.commit()
        candidates = store._candidates(db, MemoryScope(project="home"), "needle")
    assert len(candidates) <= MIDDLE_CANDIDATE_LIMIT + 256


def test_weighted_vector_top96_finds_old_scoped_vector_beyond_recent_cap(tmp_path):
    path = tmp_path / "memory.db"
    store = HybridMemoryStorage(str(path), embedder=Embedder(),
                                fusion_mode="weighted", fusion_weight=.6667)
    with sqlite3.connect(path) as db:
        rows = [("episodic", "old:target", json.dumps({"text": "unrelated target"}),
                 json.dumps({"project": "home"}), 1, 0.0)]
        rows += [("episodic", f"new:{i}", json.dumps({"text": f"unrelated {i}"}),
                  json.dumps({"project": "home"}), 1, float(i + 1)) for i in range(320)]
        rows += [("episodic", "foreign:target", json.dumps({"text": "target"}),
                  json.dumps({"project": "other"}), 1, 500.0)]
        rows += [("episodic", "stale:target", json.dumps({"text": "target"}),
                  json.dumps({"project": "home"}), 1, 501.0)]
        rows += [("episodic", "retired:target", json.dumps({"text": "target"}),
                  json.dumps({"project": "home"}), 1, 502.0)]
        db.executemany("INSERT INTO adr001_memory(layer,key,value,scope,version,created) "
                       "VALUES (?,?,?,?,?,?)", rows)
        vectors = db.execute("SELECT m.rowid,m.key,f.body FROM adr001_memory m "
                             "JOIN adr001_fts f ON f.rowid=m.rowid").fetchall()
        db.executemany("INSERT INTO adr001_vec(rowid,model_id,content_sha,vec) "
                       "VALUES (?,?,?,?)", [
            (rowid, Embedder.model_id, hashlib.sha256(body.encode()).hexdigest(),
             array("f", [1.0, 0.0] if key.endswith("target") else [0.0, 1.0]).tobytes())
            for rowid, key, body in vectors])
        db.execute("UPDATE adr001_vec SET content_sha='stale' WHERE rowid=("
                   "SELECT rowid FROM adr001_memory WHERE key='stale:target')")
        db.execute("UPDATE adr001_memory SET invalidated_at=1 WHERE key='retired:target'")
        db.commit()
    vector_rows, _ = store._weighted_vector_top(MemoryScope(project="home"), "needle")
    assert {"foreign:target", "stale:target", "retired:target"}.isdisjoint(
        {row["key"] for row in vector_rows})
    result = store.retrieve_ranked(MemoryScope(project="home"), "needle", "implementer", 5, 10000)
    assert result["mode"] == "hybrid"
    assert result["middle"][0]["key"] == "old:target"


def test_weighted_vector_matmul_under_300ms_on_live_sized_scoped_rows(tmp_path):
    path = tmp_path / "memory.db"
    store = HybridMemoryStorage(str(path), embedder=Embedder(),
                                fusion_mode="weighted", fusion_weight=.6667)
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO adr001_memory(layer,key,value,scope,version,created) "
                       "VALUES (?,?,?,?,?,?)", [
            ("episodic", f"row:{i}", json.dumps({"text": f"unrelated {i}"}),
             json.dumps({"project": "home"}), 1, float(i)) for i in range(20000)])
        rows = db.execute("SELECT m.rowid,f.body FROM adr001_memory m JOIN adr001_fts f "
                          "ON f.rowid=m.rowid").fetchall()
        blob = array("f", [1.0] + [0.0] * 383).tobytes()
        db.executemany("INSERT INTO adr001_vec(rowid,model_id,content_sha,vec) "
                       "VALUES (?,?,?,?)", [
            (rowid, Embedder.model_id, hashlib.sha256(body.encode()).hexdigest(), blob)
            for rowid, body in rows])
        db.commit()
    class WideEmbedder:
        model_id = Embedder.model_id

        def __call__(self, text):
            return [1.0] + [0.0] * 383

    store.embedder = WideEmbedder()
    for _ in range(3):
        result = store.retrieve_ranked(MemoryScope(project="home"), "needle",
                                       "implementer", 10, 10000)
        assert result["mode"] == "hybrid", result["latency_ms"]
        assert result["latency_ms"] < 300
    assert store.fallback_count == 0


def test_server_receipt_keeps_nondefault_fusion_mode_and_timeout(tmp_path, monkeypatch,
                                                                  unused_tcp_port):
    path = tmp_path / "memory.db"
    SQLiteMemoryStorage(str(path)).put(MemoryRecord(
        "episodic", "signal", {"text": "signal"}, MemoryScope(project="project-a")))
    monkeypatch.setenv("AGENT_CREW_MEMORY_BACKEND", "hybrid")
    monkeypatch.setenv("AGENT_CREW_MEMORY_DB", str(path))
    monkeypatch.setenv("AGENT_CREW_MEMORY_FUSION_MODE", "weighted")
    _, context, events, _ = _dispatch_snapshot(
        tmp_path / "dispatch", monkeypatch, NullMemoryProvider(),
        unused_tcp_port=unused_tcp_port)
    assert events[-1]["fusion_mode"] == "weighted"
    assert context["shadow_memory"]["fusion_mode"] == "weighted"

    original_rank = HybridMemoryStorage._rank_middle

    def slow_rank(self, *args):
        time.sleep(.45)
        return original_rank(self, *args)

    monkeypatch.setattr(HybridMemoryStorage, "_rank_middle", slow_rank)
    with TestClient(create_app(str(tmp_path / "http-tasks.db"), project="project-a",
                               watchdog_disabled=True, anomaly_disabled=True)) as client:
        response = client.post(
            "/memory/retrieve_ranked", headers={"X-Agent-Crew-Project": "project-a"},
            json={"scope": {"project": "project-a"}, "query": "signal",
                  "role": "implementer", "k": 10, "byte_budget": 4000})
    assert response.status_code == 200
    assert response.json()["mode"] == "fallback"
    assert response.json()["fusion_mode"] == "weighted"
