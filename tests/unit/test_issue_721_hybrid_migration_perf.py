"""#721: hybrid migration must not scan the whole corpus for every link."""

import json
import sqlite3
import time

from fastapi.testclient import TestClient

from agent_crew.memory_hybrid import HybridMemoryStorage, ensure_index_schema
from agent_crew.memory_runtime import SQLiteMemoryStorage
from agent_crew.server import create_app


def test_supersedes_migration_large_corpus_is_index_backed(tmp_path, caplog):
    path = tmp_path / "memory.db"
    SQLiteMemoryStorage(str(path))
    scope = json.dumps({"project": "agent_crew"})
    rows = []
    for index in range(900):
        rows.append(("episodic", f"old:{index}", '{"text":"old"}', scope, 1, 1.0))
    for index in range(900):
        rows.append(("episodic", f"new:{index}",
                     json.dumps({"text": "new", "supersedes": f"old:{index}"}),
                     scope, 1, 2.0))
    for index in range(26):
        rows.append(("episodic", f"newer:{index}",
                     json.dumps({"text": "newer", "supersedes": f"old:{index}"}),
                     scope, 1, 2.0))
    rows.extend(("episodic", f"filler:{index}", '{"text":"filler"}', scope, 1, 3.0)
                for index in range(10110 - len(rows)))
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO adr001_memory(layer,key,value,scope,version,created) "
                       "VALUES (?,?,?,?,?,?)", rows)
        db.commit()
        started = time.perf_counter()
        with caplog.at_level("INFO"):
            ensure_index_schema(db)
        elapsed = time.perf_counter() - started
        assert elapsed < 2.0, f"10,110-row migration took {elapsed:.2f}s"
        linked = db.execute("SELECT key,superseded_by FROM adr001_memory "
                            "WHERE superseded_by IS NOT NULL").fetchall()
        assert len(linked) == 900
        assert dict(linked)["old:0"] == "newer:0"  # created tie uses later rowid
        assert dict(linked)["old:26"] == "new:26"
        assert "superseded=900" in caplog.text
        db.commit()
        started = time.perf_counter()
        ensure_index_schema(db)
        assert time.perf_counter() - started < 2.0


def test_hybrid_initialization_uses_long_busy_timeout(monkeypatch, tmp_path):
    connect = sqlite3.connect
    timeouts = []

    def recording_connect(*args, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    HybridMemoryStorage(str(tmp_path / "memory.db"))
    assert timeouts[:2] == [30.0, 30.0]


def test_hybrid_startup_fallback_is_visible_in_health(monkeypatch, tmp_path, caplog):
    path = tmp_path / "memory.db"
    SQLiteMemoryStorage(str(path))
    monkeypatch.setenv("AGENT_CREW_MEMORY_BACKEND", "hybrid")
    monkeypatch.setenv("AGENT_CREW_MEMORY_DB", str(path))

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("agent_crew.memory_hybrid.HybridMemoryStorage", locked)
    app = create_app(str(tmp_path / "tasks.db"), watchdog_disabled=True,
                     anomaly_disabled=True)
    health = TestClient(app).get("/health").json()
    assert health["hybrid_memory"] == {"available": False, "startup_fallback_count": 1}
    assert "hybrid memory unavailable: database is locked" in caplog.text
