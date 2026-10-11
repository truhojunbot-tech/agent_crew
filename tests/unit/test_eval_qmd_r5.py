"""Local invariants for the isolated ADR-001 QMD evaluation export."""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from eval_qmd_r5 import backup, export_rows, qmd_env, summarize_results, vector_query, virtual_manifest


def test_backup_and_export_only_effective_rows(tmp_path):
    source = tmp_path / "live.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE adr001_memory (layer TEXT, key TEXT, value TEXT, scope TEXT, "
                   "superseded_by TEXT, invalidated_at TEXT)")
        rows = [
            ("procedural", "owner:one", json.dumps({"text": "한글 rule"}),
             json.dumps({"project": "Quota"}), None, None),
            ("procedural", "owner:old", json.dumps({"text": "old"}),
             json.dumps({"project": "Quota"}), "owner:one", None),
            ("episodic", "episode:dead", json.dumps({"text": "dead"}),
             json.dumps({"fleet": "fleet"}), None, "2026-01-01"),
            ("procedural", "owner_principle:fleet", json.dumps({"text": "fleet rule"}),
             json.dumps({"fleet": "fleet"}), None, None),
        ]
        db.executemany("INSERT INTO adr001_memory VALUES (?,?,?,?,?,?)", rows)
    copy = tmp_path / "snapshot.db"
    backup(source, copy)
    manifest, stats = export_rows(copy, tmp_path / "docs")
    assert stats["effective_rows"] == 2
    assert stats["projects"] == ["Quota"]
    assert {row["key"] for row in manifest.values()} == {"owner:one", "owner_principle:fleet"}
    virtual = virtual_manifest(manifest)
    assert virtual["qmd://adr001_project_Quota/00000001.md"]["korean"]
    assert virtual["qmd://adr001_project_Quota/fleet_00000004.md"]["key"] == "owner_principle:fleet"
    fleet = tmp_path / "docs/fleet/fleet/00000004.md"
    alias = tmp_path / "docs/project/Quota/fleet_00000004.md"
    assert fleet.stat().st_ino == alias.stat().st_ino
    assert "key: \"owner:one\"" in (tmp_path / "docs/project/Quota/00000001.md").read_text()
    with sqlite3.connect(source) as db:
        assert db.execute("SELECT count(*) FROM adr001_memory").fetchone()[0] == 4


def test_summary_uses_first_run_recall_and_all_run_latencies():
    samples = [
        {"rep": 0, "kind": "owner", "korean_target": True, "hit": True, "latency_ms": 10},
        {"rep": 0, "kind": "task", "korean_target": False, "hit": False, "latency_ms": 20},
        {"rep": 1, "kind": "owner", "korean_target": True, "hit": False, "latency_ms": 30},
        {"rep": 1, "kind": "task", "korean_target": False, "hit": True, "latency_ms": 40},
    ]
    summary = summarize_results({"arms": {"search": samples}})["search"]
    assert summary == {"owner_hits": 1, "task_hits": 0, "owner_korean_hits": 1,
                       "owner_english_hits": 0, "p50_ms": 20, "p95_ms": 40,
                       "samples": 4}


def test_qmd_index_and_config_are_private(tmp_path):
    env = qmd_env(tmp_path)
    assert env["INDEX_PATH"] == str(tmp_path / "index.sqlite")
    assert env["QMD_CONFIG_DIR"] == str(tmp_path / "config")


def test_vector_query_removes_only_unsupported_negation():
    assert vector_query("cross-project obligation -archived") == "cross project obligation"
