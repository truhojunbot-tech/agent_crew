"""Bounded read-only layer retrieval (#1088-Q)."""
import hashlib
import json
import shutil
import sqlite3
from dataclasses import asdict

import pytest

from agent_crew.memory_runtime import MemoryRecord, MemoryScope, SQLiteMemoryStorage
from agent_crew.memory_hybrid import HybridMemoryStorage

SCOPE = MemoryScope(project="athena")


def put(store, key, *, layer="episodic", scope=SCOPE, text="signal"):
    store.put(MemoryRecord(layer, key, {"text": text}, scope))


def readonly(store, tmp_path):
    path = tmp_path / "readonly.db"
    shutil.copyfile(store.path, path)
    path.chmod(0o444)
    return SQLiteMemoryStorage.existing(str(path)), path


@pytest.mark.parametrize("layer", ["episodic", "procedural"])
def test_full_history_and_scope_readonly(tmp_path, layer):
    store = HybridMemoryStorage(str(tmp_path / "source.db"))
    put(store, "old", layer=layer, scope=MemoryScope(project="athena", issue="758"), text="rareterm")
    for i in range(60):
        put(store, f"new{i}", layer=layer, text="ordinary")
    put(store, "checkpoint", layer="checkpoint", text="rareterm")
    put(store, "foreign", layer=layer, scope=MemoryScope(project="other"), text="rareterm")
    reader, path = readonly(store, tmp_path)
    before = hashlib.sha256(path.read_bytes()).digest()
    result = reader.retrieve_layer_ranked(SCOPE, layer, "rareterm", 10, 10000)
    assert result["status"] == "AVAILABLE"
    assert [r.key for r in result["rows"]] == ["old"]
    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert reader.retrieve(SCOPE, layers={layer})
    assert hashlib.sha256(path.read_bytes()).digest() == before


@pytest.mark.parametrize("hybrid", [False, True])
def test_bounds_and_effectiveness(tmp_path, hybrid):
    cls = HybridMemoryStorage if hybrid else SQLiteMemoryStorage
    store = cls(str(tmp_path / "source.db"))
    for key in ("a", "b", "invalid", "superseded"):
        put(store, key)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE adr001_memory SET invalidated_at=1 WHERE key='invalid'")
        db.execute("UPDATE adr001_memory SET superseded_by='b' WHERE key='superseded'")
    reader, path = readonly(store, tmp_path)
    before = path.read_bytes()
    result = reader.retrieve_layer_ranked(SCOPE, "episodic", "signal", 1, 10000)
    assert result["status"] == "AVAILABLE" and result["truncated"]
    assert len(result["rows"]) == 1
    assert result["rows"][0].key in {"a", "b"}
    assert result["bytes"] == len(json.dumps(asdict(result["rows"][0]), ensure_ascii=False,
                                            sort_keys=True).encode())
    assert path.read_bytes() == before
    result = reader.retrieve_layer_ranked(SCOPE, "episodic", "signal", 10, 1)
    assert result["rows"] == [] and result["truncated"]
    assert result["bytes"] == 0
    assert path.read_bytes() == before


def test_missing_database_fails_closed(tmp_path):
    store = SQLiteMemoryStorage(str(tmp_path / "source.db"))
    store.path = str(tmp_path / "missing.db")
    result = store.retrieve_layer_ranked(SCOPE, "episodic", "", 10, 10000)
    assert result["status"] == "UNKNOWN" and result["rows"] == []
    assert not (tmp_path / "missing.db").exists()


def test_procedural_and_fallback_bound(tmp_path):
    store = SQLiteMemoryStorage(str(tmp_path / "source.db"))
    put(store, "fleet", layer="procedural", scope=MemoryScope(fleet="shared"))
    store.put(MemoryRecord("procedural", "pending", {"lifecycle": {"status": "pending"}}, SCOPE))
    result = store.retrieve_layer_ranked(SCOPE, "procedural", "", 10, 10000)
    assert [r.key for r in result["rows"]] == ["fleet"]
    for i in range(51):
        put(store, str(i))
    result = store.retrieve_layer_ranked(SCOPE, "episodic", "signal", 10, 10000)
    assert result["status"] == "UNKNOWN" and result["reason"] == "budget"
    assert result["rows"] == []


def test_changed_reads_enforce_ro_and_owner_results(tmp_path, monkeypatch):
    from agent_crew.memory_runtime import capture_owner_statement, effective_owner_statements
    from agent_crew.memory_hybrid import _row_record
    store = HybridMemoryStorage(str(tmp_path / "source.db"))
    first = None
    for mid, text in (("1", "Friday"), ("2", "Monday")):
        record = capture_owner_statement(
            store, project="athena", target_project="athena", text=text, source_ref="test",
            proof={"status": "VERIFIED", "chat_id": "42", "user_id": "42",
                   "message_id": mid, "ts": "now",
                   "text_sha256": hashlib.sha256(text.encode()).hexdigest()},
            supersedes=[first.key] if first else [])
        first = first or record
    put(store, "checkpoint", layer="checkpoint")
    put(store, "episode")
    expected = effective_owner_statements(store, "athena")
    reader, path = readonly(store, tmp_path)
    hybrid = HybridMemoryStorage.__new__(HybridMemoryStorage)
    hybrid.path, hybrid.embedder = reader.path, None
    with sqlite3.connect(reader.path) as db:
        candidates = [_row_record(r) for r in db.execute(
            "SELECT rowid,layer,key,value,scope,version,created FROM adr001_memory "
            "WHERE layer='episodic'")]
    connect = sqlite3.connect
    statements = []

    def connect_ro(database, **kwargs):
        assert database.endswith("?mode=ro") and kwargs.get("uri") is True
        db = connect(database, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("CREATE TABLE forbidden_write(x)")
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", connect_ro)
    digest = hashlib.sha256(path.read_bytes()).digest()
    for read in (
        lambda: reader.retrieve(SCOPE, layers={"episodic"}),
        lambda: reader.retrieve_layer_ranked(SCOPE, "episodic", "signal", 10, 10000),
        lambda: effective_owner_statements(reader, "athena"),
        lambda: hybrid.render_head("athena"),
        lambda: hybrid._rank_middle(candidates, "signal"),
    ):
        result = read()
        assert hashlib.sha256(path.read_bytes()).digest() == digest
        if isinstance(result, list) and result and result[0].layer == "authoritative":
            assert result == expected
    assert any("layer IN ('authoritative')" in sql for sql in statements)


def test_deadline_discards_rows(tmp_path, monkeypatch):
    import agent_crew.memory_runtime as runtime
    store = SQLiteMemoryStorage(str(tmp_path / "source.db"))
    put(store, "a")
    ticks = iter([0, 0, 1, 1, 1])
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(ticks, 1))
    result = store.retrieve_layer_ranked(SCOPE, "episodic", "signal", 10, 10000)
    assert result == {"status": "UNKNOWN", "reason": "deadline", "rows": [],
                      "bytes": 0, "truncated": False}


def test_legacy_columns_and_empty_layer_filter(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE adr001_memory(layer,key,value,scope,version,created)")
        db.execute("INSERT INTO adr001_memory VALUES(?,?,?,?,?,?)",
                   ("episodic", "old", '{"text":"signal"}', json.dumps(asdict(SCOPE)), 1, 0))
    reader = SQLiteMemoryStorage.existing(str(path))
    assert reader.retrieve_layer_ranked(SCOPE, "episodic", "signal", 1, 10000)["status"] == "AVAILABLE"
    assert reader.retrieve(SCOPE, layers=set()) == []


@pytest.mark.parametrize("hybrid", [False, True])
@pytest.mark.parametrize("query", ["Friday", ""])
def test_ranked_authority_rejected_without_database_access(tmp_path, monkeypatch, hybrid, query):
    cls = HybridMemoryStorage if hybrid else SQLiteMemoryStorage
    store = cls(str(tmp_path / "source.db"))

    def forbidden_connect(*args, **kwargs):
        pytest.fail("ranked authority must not open or scan the database")

    monkeypatch.setattr(sqlite3, "connect", forbidden_connect)
    for k, budget in ((0, 0), (10, 10000)):
        result = store.retrieve_layer_ranked(SCOPE, "authoritative", query, k, budget)
        assert result == {
            "status": "UNKNOWN", "reason": "AUTHORITY_VIA_EFFECTIVE_OWNER_STATEMENTS",
            "rows": [], "bytes": 0, "truncated": False,
        }


@pytest.mark.parametrize("hybrid", [False, True])
def test_owner_api_remains_complete_beyond_shadow_bound(tmp_path, hybrid):
    from agent_crew.memory_runtime import capture_owner_statement, effective_owner_statements
    cls = HybridMemoryStorage if hybrid else SQLiteMemoryStorage
    store = cls(str(tmp_path / "source.db"))
    expected = set()
    first = None
    for i in range(60):
        text = "Friday" if i == 0 else "Monday"
        record = capture_owner_statement(
            store, project="athena", target_project="athena", text=text, source_ref="test",
            proof={"status": "VERIFIED", "chat_id": "42", "user_id": "42",
                   "message_id": str(i), "ts": "now",
                   "text_sha256": hashlib.sha256(text.encode()).hexdigest()},
            supersedes=[first.key] if i == 1 else [])
        first = first or record
        expected.add(record.key)
    expected.remove(first.key)
    put(store, "checkpoint", layer="checkpoint")
    put(store, "foreign", layer="authoritative", scope=MemoryScope(project="other"))
    reader, path = readonly(store, tmp_path)
    before = path.read_bytes()
    rows = effective_owner_statements(reader, "athena")
    assert {r.key for r in rows} == expected
    assert all(r.layer == "authoritative" for r in rows)
    assert path.read_bytes() == before


@pytest.mark.parametrize("hybrid", [False, True])
@pytest.mark.parametrize("layer", ["decision", "failure_pattern", "checkpoint", "unknown"])
def test_unsupported_layer_rejected_without_database_access(tmp_path, monkeypatch, hybrid, layer):
    cls = HybridMemoryStorage if hybrid else SQLiteMemoryStorage
    store = cls(str(tmp_path / "source.db"))
    if layer != "unknown":
        put(store, "present", layer=layer)

    def forbidden_connect(*args, **kwargs):
        pytest.fail("unsupported layer must not open the database")

    monkeypatch.setattr(sqlite3, "connect", forbidden_connect)
    assert store.retrieve_layer_ranked(SCOPE, layer, "", 100, 100000) == {
        "status": "UNKNOWN", "reason": "UNSUPPORTED_LAYER", "rows": [],
        "bytes": 0, "truncated": False,
    }
