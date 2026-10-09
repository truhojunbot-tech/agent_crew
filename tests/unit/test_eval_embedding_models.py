"""The frozen embedding evaluation remains reproducible and source read-only."""

import hashlib
import json
import sqlite3

import pytest

from scripts.eval_embedding_models import (
    EvalOnnxEmbedder, choose_model, copy_database, count_available_keys,
    download_model, load_eval_set, score_hit, score_model,
)
from agent_crew.memory_hybrid import HybridMemoryStorage
from agent_crew.memory_runtime import MemoryRecord, MemoryScope


def test_eval_set_hash_is_checked_before_parsing(tmp_path):
    path = tmp_path / "eval.json"
    raw = json.dumps({"owner_cases": [], "task_cases": []}).encode()
    path.write_bytes(raw)
    assert load_eval_set(path, hashlib.sha256(raw).hexdigest())["owner_cases"] == []
    with pytest.raises(ValueError, match="sha256"):
        load_eval_set(path, "0" * 64)


def test_copy_database_uses_sqlite_backup_and_leaves_source_untouched(tmp_path):
    source, dest = tmp_path / "source.db", tmp_path / "copy.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE facts (value TEXT)")
        db.execute("INSERT INTO facts VALUES ('original')")
    before = source.read_bytes()
    copy_database(source, dest)
    with sqlite3.connect(dest) as db:
        db.execute("INSERT INTO facts VALUES ('copy only')")
        db.commit()
    assert source.read_bytes() == before
    with sqlite3.connect(source) as db:
        assert db.execute("SELECT count(*) FROM facts").fetchone()[0] == 1


def test_score_hit_counts_a_case_once_and_requires_exact_key():
    response = {"head": [{"key": "owner:key"}], "middle": [{"key": "second"}]}
    assert score_hit(["owner:key", "second"], response) is True
    assert score_hit(["owner"], response) is False
    assert score_hit([{"ref": "second"}], response) is True


def test_choose_model_enforces_retrieve_budget_and_uses_recall():
    rows = [
        {"model_id": "slow", "owner_hits": 17, "task_hits": 24,
         "p95_retrieve_ms": 301, "model_size_bytes": 1},
        {"model_id": "fast", "owner_hits": 10, "task_hits": 5,
         "p95_retrieve_ms": 250, "model_size_bytes": 2},
        {"model_id": "better", "owner_hits": 11, "task_hits": 5,
         "p95_retrieve_ms": 299, "model_size_bytes": 3},
        {"model_id": "all-MiniLM-L6-v2", "owner_hits": 17, "task_hits": 24,
         "p95_retrieve_ms": 100, "model_size_bytes": 1},
        {"model_id": "lexical_only", "owner_hits": 17, "task_hits": 24,
         "p95_retrieve_ms": 1, "model_size_bytes": 0},
    ]
    assert choose_model(rows)["model_id"] == "better"


def test_english_control_downloads_to_external_cache_when_chroma_copy_absent(
        tmp_path, monkeypatch):
    import huggingface_hub
    import scripts.eval_embedding_models as evaluator

    monkeypatch.setattr(evaluator.Path, "home", lambda: tmp_path)
    cache = tmp_path / "model-cache"
    cache.mkdir()
    model = cache / "model.onnx"
    tokenizer = cache / "tokenizer.json"
    model.write_bytes(b"model")
    tokenizer.write_bytes(b"tokenizer")
    calls = []

    def fake_download(repo_id, filename, *, cache_dir):
        calls.append((repo_id, filename, cache_dir))
        return str(model if filename == "onnx/model.onnx" else tokenizer)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)
    monkeypatch.setattr(evaluator, "EvalOnnxEmbedder",
                        lambda model_path, tokenizer_path, model_id, query_texts: model_id)
    embedder, size = download_model("all-MiniLM-L6-v2", cache, set())
    assert embedder == "all-MiniLM-L6-v2"
    assert size == len(b"modeltokenizer")
    assert calls == [
        ("sentence-transformers/all-MiniLM-L6-v2", "onnx/model.onnx", str(cache)),
        ("sentence-transformers/all-MiniLM-L6-v2", "tokenizer.json", str(cache)),
    ]


def test_score_model_preembeds_every_row_before_scoring(tmp_path, monkeypatch):
    import scripts.eval_embedding_models as evaluator

    source = tmp_path / "source.db"
    storage = HybridMemoryStorage(str(source), embedder=lambda text: [1.0, 0.0])
    for key, text in (("owner:one", "alpha keyword"),
                      ("owner:two", "beta keyword"),
                      ("owner:three", "gamma keyword")):
        storage.put(MemoryRecord("authoritative", key, {"text": text},
                                 MemoryScope(project="sample")))

    class FakeEmbedder:
        model_id = "fake"
        timings_ms = [1.0]
        batches = 0

        def __call__(self, text):
            return [1.0, 0.0]

        def embed_many(self, texts):
            self.batches += 1
            return [[1.0, 0.0] for _ in texts]

    fake = FakeEmbedder()
    monkeypatch.setattr(evaluator, "download_model", lambda *args: (fake, 42))
    cases = {"owner_cases": [{"project": "sample", "query": "alpha",
                               "expected": ["owner:one"]}],
             "task_cases": [{"project": "sample", "query": "missing",
                             "expected": ["absent"]}]}
    assert count_available_keys(source, cases["owner_cases"]) == 1
    assert count_available_keys(source, cases["task_cases"]) == 0
    row = score_model(source, tmp_path, "fake", cases, tmp_path,
                      k=10, byte_budget=65536)
    assert (row["owner_hits"], row["task_hits"]) == (1, 0)
    assert row["retrieval_modes"] == {"hybrid": 2}
    assert row["vector_coverage"] == {"embedded": 3, "total": 3}
    assert fake.batches == 1
    resumed = score_model(source, tmp_path, "fake", cases, tmp_path,
                          k=10, byte_budget=65536, resume=True)
    assert resumed["vector_coverage"] == {"embedded": 3, "total": 3}
    assert fake.batches == 1


def test_e5_applies_query_and_passage_prefixes():
    import numpy as np

    encoded = []

    class Tokenizer:
        def encode(self, text):
            encoded.append(text)
            return type("Encoding", (), {"ids": [1, 2]})()

    class Session:
        def get_inputs(self):
            return [type("Input", (), {"name": "input_ids"})()]

        def run(self, _, feeds):
            return [np.asarray([[1.0, 0.0]] * len(feeds["input_ids"]),
                               dtype=np.float32)]

    embedder = EvalOnnxEmbedder.__new__(EvalOnnxEmbedder)
    embedder.session = Session()
    embedder.tokenizer = Tokenizer()
    embedder.model_id = "intfloat/multilingual-e5-small"
    embedder.query_texts = {"find this"}
    embedder.timings_ms = []
    embedder("find this")
    embedder("stored passage")
    embedder.embed_many(["find this", "stored passage"])
    assert encoded == ["query: find this", "passage: stored passage",
                       "passage: find this", "passage: stored passage"]
