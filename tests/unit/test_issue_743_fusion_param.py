"""#743: scoped vector and FTS arms with configurable fusion."""
from array import array
import hashlib
import json
import sqlite3

from agent_crew.memory_hybrid import HybridMemoryStorage
from agent_crew.memory_runtime import MemoryScope


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
