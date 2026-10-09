"""#671: effective export and scoped hybrid retrieval."""
import json
from io import BytesIO

from agent_crew.memory_runtime import (
    HybridMemoryStorage, MemoryRecord, MemoryScope, SQLiteMemoryStorage,
    export_effective_records, memory_storage_from_env,
)


def record(key, project, *, value=None, layer="episodic"):
    return MemoryRecord(layer, key, value or {"text": key}, MemoryScope(project=project))


def test_effective_export_drops_superseded_and_invalidated(tmp_path):
    store = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    store.put(record("a", "one"))
    store.put(record("b", "one", value={"superseded_at": 1}))
    store.put(record("c", "one", value={"invalidated_at": 1}))
    store.put(record("owner:old", "one", layer="authoritative"))
    store.put(record("owner:new", "one", layer="authoritative",
                     value={"supersedes": ["owner:old"]}))
    rows = list(export_effective_records(store))
    assert [row["key"] for row in rows] == ["owner:new", "a"]
    assert rows[1]["metadata"] == {
        "id": rows[1]["id"], "layer": "episodic", "bot": "",
        "project": "one", "key": "a", "superseded_by": None,
    }


def test_hybrid_rejects_other_project_and_superseded_forge_hits(tmp_path, monkeypatch):
    store = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    store.put(record("own", "one"))
    store.put(record("foreign", "two"))
    store.put(record("old", "one", value={"superseded_at": 1}))
    ids = {row["key"]: row["id"] for row in export_effective_records(store)}
    sent = []

    class Response(BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): self.close()

    def fake_open(request, timeout):
        sent.append(json.loads(request.data))
        return Response(json.dumps({"context_items": [
            {"id": ids["foreign"]}, {"id": "old"}, {"id": ids["own"]},
        ]}).encode())

    monkeypatch.setattr("agent_crew.memory_runtime.urllib.request.urlopen", fake_open)
    hybrid = HybridMemoryStorage(store, forge_url="http://forge")
    assert [r.key for r in hybrid.retrieve(MemoryScope(project="one"), "own")] == ["own"]
    assert sent[0]["candidate_ids"] == [ids["own"]]
    assert hybrid.last_retrieval_mode == "hybrid"


def test_forge_failure_equals_sqlite_and_env_defaults_to_sqlite(tmp_path, monkeypatch):
    path = str(tmp_path / "memory.db")
    store = SQLiteMemoryStorage(path)
    store.put(record("first", "one"))
    store.put(record("second", "one"))
    monkeypatch.delenv("AGENT_CREW_MEMORY_BACKEND", raising=False)
    assert isinstance(memory_storage_from_env(path), SQLiteMemoryStorage)
    monkeypatch.setenv("AGENT_CREW_MEMORY_BACKEND", "hybrid")
    monkeypatch.setenv("AGENT_CREW_FORGE_URL", "http://forge")
    hybrid = memory_storage_from_env(path)
    assert isinstance(hybrid, HybridMemoryStorage)

    def down(*args, **kwargs):
        raise TimeoutError("Forge down")

    monkeypatch.setattr("agent_crew.memory_runtime.urllib.request.urlopen", down)
    assert hybrid.retrieve(MemoryScope(project="one"), "second") == store.retrieve(
        MemoryScope(project="one"), "second")
    assert hybrid.last_retrieval_mode == "lexical_fallback"
