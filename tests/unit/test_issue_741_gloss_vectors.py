"""E5 prefix and dual-vector serving contract for issue #741."""
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import numpy as np

from agent_crew.memory_hybrid import (EndpointEmbedder, HybridMemoryStorage,
                                      OnnxMiniLMEmbedder, configured_write_embedder)
from agent_crew.memory_runtime import MemoryRecord, MemoryScope


class KindEmbedder:
    model_id = "e5-fixture"
    query_prefix = "query: "
    passage_prefix = "passage: "

    def __init__(self):
        self.calls = []

    def embed_kind(self, text, kind):
        self.calls.append((kind, text))
        if kind == "query":
            return [0.0, 1.0]
        return [0.0, 1.0] if "gloss target" in text else [1.0, 0.0]

    def __call__(self, text):
        return self.embed_kind(text, "passage")


def test_onnx_prefix_masked_mean_and_l2(monkeypatch):
    seen = []
    class Tokenizer:
        @staticmethod
        def from_file(_):
            def encode(text):
                seen.append(text)
                return SimpleNamespace(ids=[1, 2], attention_mask=[1, 0])
            return SimpleNamespace(encode=encode)

    class Session:
        def __init__(self, *_args, **_kwargs):
            self.feeds = None

        def get_inputs(self):
            return [SimpleNamespace(name=name) for name in
                    ("input_ids", "attention_mask", "token_type_ids")]

        def run(self, _outputs, feeds):
            self.feeds = feeds
            return [np.array([[[3.0, 4.0], [100.0, 0.0]]], dtype=np.float32)]

    session = Session()
    monkeypatch.setitem(__import__("sys").modules, "onnxruntime",
                        SimpleNamespace(InferenceSession=lambda *_a, **_k: session))
    monkeypatch.setitem(__import__("sys").modules, "tokenizers", SimpleNamespace(Tokenizer=Tokenizer))
    embedder = OnnxMiniLMEmbedder("model", "tokens", "e5", query_prefix="query: ", passage_prefix="passage: ")
    assert np.allclose(embedder.embed_kind("test", "query"), [0.6, 0.8])
    assert session.feeds["attention_mask"].tolist() == [[1, 0]]
    assert np.allclose(embedder.embed_kind("test", "passage"), [0.6, 0.8])
    assert seen == ["query: test", "passage: test"]


def test_endpoint_applies_kind_prefix(monkeypatch):
    requests = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, *_): return b'{"embeddings": [[3.0, 4.0]]}'
    def open_request(request, timeout):
        requests.append(json.loads(request.data)["input"])
        return Response()
    monkeypatch.setattr("agent_crew.memory_hybrid.urllib.request.urlopen", open_request)
    embedder = EndpointEmbedder("http://localhost/embed", "e5", query_prefix="query: ", passage_prefix="passage: ")
    assert embedder.embed_kind("hello", "query") == [3.0, 4.0]
    assert embedder.embed_kind("hello", "passage") == [3.0, 4.0]
    assert requests == ["query: hello", "passage: hello"]


def test_gloss_vector_max_cosine_ranks_gloss_only_match_first(tmp_path):
    embedder = KindEmbedder()
    store = HybridMemoryStorage(str(tmp_path / "memory.db"), embedder=embedder)
    scope = MemoryScope(project="test")
    store.put(MemoryRecord("episodic", "a-gloss", {"text": "unrelated Korean", "gloss_en": "gloss target"}, scope))
    store.put(MemoryRecord("episodic", "z-plain", {"text": "other unrelated", "gloss_en": "other gloss"}, scope))
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM adr001_vec_gloss").fetchone()[0] == 2
    result = store.retrieve_ranked(scope, "semantic only", "implementer", 10, 10000)
    assert result["middle"][0]["key"] == "a-gloss"
    assert ("query", "semantic only") in embedder.calls


def test_backfill_covers_gloss_model_and_stale_digest(tmp_path):
    path = str(tmp_path / "memory.db")
    scope = MemoryScope(project="test")
    store = HybridMemoryStorage(path)
    store.put(MemoryRecord("episodic", "dual", {"text": "original", "gloss_en": "gloss target"}, scope))
    embedder = KindEmbedder()
    store = HybridMemoryStorage(path, embedder=embedder)
    assert store.remaining_vector_count() == 2
    assert store.backfill_vectors() == 1
    assert store.remaining_vector_count() == 0
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT model_id FROM adr001_vec_gloss").fetchone()[0] == embedder.model_id
        db.execute("UPDATE adr001_vec_gloss SET content_sha='stale'")
        db.commit()
    assert store.remaining_vector_count() == 1
    assert store.backfill_vectors() == 1
    assert store.remaining_vector_count() == 0


def test_minilm_default_prefix_is_empty_and_legacy_body_hash(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_MEMORY_QUERY_PREFIX", raising=False)
    monkeypatch.delenv("AGENT_CREW_MEMORY_PASSAGE_PREFIX", raising=False)
    class PlainEmbedder:
        model_id = "minilm-default"
        def __call__(self, text): return [1.0, 0.0]
    embedder = PlainEmbedder()
    store = HybridMemoryStorage(str(tmp_path / "memory.db"), embedder=embedder)
    store.put(MemoryRecord("episodic", "plain", {"text": "plain"}, MemoryScope(project="test")))
    with sqlite3.connect(store.path) as db:
        body, digest = db.execute("SELECT f.body,v.content_sha FROM adr001_fts f JOIN adr001_vec v ON v.rowid=f.rowid").fetchone()
    assert digest == hashlib.sha256(body.encode()).hexdigest()


def test_configured_endpoint_prefixes_are_opt_in(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_MEMORY_EMBED_ENDPOINT", "http://127.0.0.1/embed")
    monkeypatch.setenv("AGENT_CREW_MEMORY_MODEL_ID", "e5")
    monkeypatch.setenv("AGENT_CREW_MEMORY_QUERY_PREFIX", "query: ")
    monkeypatch.setenv("AGENT_CREW_MEMORY_PASSAGE_PREFIX", "passage: ")
    configured = configured_write_embedder()
    assert (configured.query_prefix, configured.passage_prefix) == ("query: ", "passage: ")
    monkeypatch.delenv("AGENT_CREW_MEMORY_QUERY_PREFIX")
    monkeypatch.delenv("AGENT_CREW_MEMORY_PASSAGE_PREFIX")
    default = configured_write_embedder()
    assert (default.query_prefix, default.passage_prefix) == ("", "")


def test_retiring_or_deleting_row_clears_both_vectors(tmp_path):
    store = HybridMemoryStorage(str(tmp_path / "memory.db"), embedder=KindEmbedder())
    scope = MemoryScope(project="test")
    for key in ("retired", "deleted"):
        store.put(MemoryRecord("episodic", key, {"text": "body", "gloss_en": "gloss target"}, scope))
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE adr001_memory SET invalidated_at=123 WHERE key='retired'")
        db.execute("DELETE FROM adr001_memory WHERE key='deleted'")
        db.commit()
        assert db.execute("SELECT count(*) FROM adr001_vec").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM adr001_vec_gloss").fetchone()[0] == 0
