"""The frozen embedding evaluation remains reproducible and source read-only."""

import hashlib
import json
import sqlite3

import pytest

from scripts.eval_embedding_models import (
    choose_model, copy_database, load_eval_set, score_hit,
)


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
