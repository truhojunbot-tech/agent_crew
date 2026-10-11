"""Embed-at-write and resumable ADR-001 vector backfill (#741)."""
import hashlib
import json
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from unittest.mock import patch

from agent_crew.memory_hybrid import EndpointEmbedder, HybridMemoryStorage, configured_write_embedder
from agent_crew.memory_runtime import MemoryRecord, MemoryScope, SQLiteMemoryStorage
from agent_crew.memory_capture import capture_result_best_effort
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from scripts import backfill_memory_vectors


class StubEmbedder:
    def __init__(self, model_id="fixture-v1", fail=False):
        self.model_id = model_id
        self.fail = fail
        self.calls = []

    def __call__(self, text):
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("offline")
        return [1.0, 0.5]


def _put(store, key, text):
    store.put(MemoryRecord("episodic", key, {"text": text}, MemoryScope(project="test")))


def _vectors(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT m.key,f.body,v.model_id,v.content_sha FROM adr001_memory m "
                          "JOIN adr001_fts f ON f.rowid=m.rowid "
                          "LEFT JOIN adr001_vec v ON v.rowid=m.rowid ORDER BY m.key").fetchall()


def test_three_writes_embed_matching_body_sha_and_model(tmp_path):
    embedder = StubEmbedder()
    store = HybridMemoryStorage(str(tmp_path / "memory.db"), embedder=embedder)
    for index in range(3):
        _put(store, f"task:{index}", f"body {index}")
    rows = _vectors(store.path)
    assert len(embedder.calls) == 3
    assert len(rows) == 3
    assert all(model == "fixture-v1" and digest == hashlib.sha256(body.encode()).hexdigest()
               for _, body, model, digest in rows)
    _put(store, "task:0", "body 0")
    assert len(embedder.calls) == 3


def test_backfill_refreshes_model_once_and_resumes(tmp_path):
    path = str(tmp_path / "memory.db")
    first = HybridMemoryStorage(path, embedder=StubEmbedder())
    for index in range(3):
        _put(first, f"task:{index}", f"body {index}")
    newer = StubEmbedder("fixture-v2")
    store = HybridMemoryStorage(path, embedder=newer)
    assert store.backfill_vectors(limit=1) == 1
    assert store.backfill_vectors() == 2
    assert store.backfill_vectors() == 0
    assert len(newer.calls) == 3
    assert {row[2] for row in _vectors(path)} == {"fixture-v2"}


def test_embed_failure_keeps_write_for_later_backfill(tmp_path):
    embedder = StubEmbedder(fail=True)
    store = HybridMemoryStorage(str(tmp_path / "memory.db"), embedder=embedder)
    _put(store, "task:failed", "saved")
    assert _vectors(store.path)[0][2] is None
    embedder.fail = False
    assert store.backfill_vectors() == 1
    assert _vectors(store.path)[0][2] == embedder.model_id


def test_unset_endpoint_does_not_embed_at_write(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_MEMORY_EMBED_ENDPOINT", raising=False)
    monkeypatch.delenv("AGENT_CREW_MEMORY_MODEL_ID", raising=False)
    store = HybridMemoryStorage(str(tmp_path / "memory.db"))
    _put(store, "task:off", "saved")
    assert store.backfill_vectors() == 0
    assert _vectors(store.path)[0][2] is None


def test_configured_endpoint_sends_model_and_input(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_MEMORY_EMBED_ENDPOINT", "http://127.0.0.1:9999/api/embed")
    monkeypatch.setenv("AGENT_CREW_MEMORY_MODEL_ID", "chosen-model")
    embedder = configured_write_embedder()
    assert isinstance(embedder, EndpointEmbedder)

    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def fake_open(request, timeout):
        assert request.full_url == "http://127.0.0.1:9999/api/embed"
        assert json.loads(request.data) == {"model": "chosen-model", "input": "body"}
        assert timeout == 30
        return Response(b'{"embeddings": [[1.0, 0.5]]}')

    with patch("agent_crew.memory_hybrid.urllib.request.urlopen", side_effect=fake_open):
        assert embedder("body") == [1.0, 0.5]


def test_shadow_batch_embeds_after_committed_write(tmp_path):
    embedder = StubEmbedder()
    store = HybridMemoryStorage(str(tmp_path / "memory.db"), embedder=embedder)
    records = [MemoryRecord("episodic", f"task:{i}", {"text": f"body {i}"},
                            MemoryScope(project="test")) for i in range(3)]
    store.put_many_shadow(records)
    assert len(embedder.calls) == 3
    assert all(row[2] == embedder.model_id for row in _vectors(store.path))


def test_backfill_covers_all_effective_rows_on_db_copy(tmp_path):
    source = tmp_path / "source.db"
    original = SQLiteMemoryStorage(str(source))
    for index in range(12):
        _put(original, f"task:{index}", f"body {index}")
    copy = tmp_path / "copy.db"
    with sqlite3.connect(source) as src, sqlite3.connect(copy) as dst:
        src.backup(dst)
    embedder = StubEmbedder()
    store = HybridMemoryStorage(str(copy), embedder=embedder)
    with sqlite3.connect(copy) as db:
        db.execute("UPDATE adr001_memory SET invalidated_at=123 WHERE key='task:0'")
        db.execute("UPDATE adr001_memory SET superseded_by='task:2' WHERE key='task:1'")
        db.commit()
    assert store.backfill_vectors(limit=4) == 4
    assert store.backfill_vectors() == 6
    assert store.backfill_vectors() == 0
    with sqlite3.connect(copy) as db:
        covered = db.execute("SELECT count(*) FROM adr001_memory m JOIN adr001_vec v "
                             "ON m.rowid=v.rowid WHERE m.invalidated_at IS NULL "
                             "AND m.superseded_by IS NULL").fetchone()[0]
    assert covered == 10


def test_live_result_capture_embeds_with_configured_hybrid_backend(tmp_path, monkeypatch):
    path = str(tmp_path / "memory.db")
    HybridMemoryStorage(path)
    task_db = str(tmp_path / "tasks.db")
    queue = TaskQueue(task_db)
    queue.enqueue(TaskRequest(task_id="live-result", task_type="implement",
                              description="capture", project="agent_crew", context={}))
    embedder = StubEmbedder()
    monkeypatch.setenv("AGENT_CREW_SHADOW_MEMORY_DB", path)
    monkeypatch.setenv("AGENT_CREW_MEMORY_BACKEND", "hybrid")
    monkeypatch.setenv("AGENT_CREW_MEMORY_EMBED_ENDPOINT", "http://127.0.0.1:9999/embed")
    monkeypatch.setenv("AGENT_CREW_MEMORY_MODEL_ID", embedder.model_id)
    monkeypatch.setattr("agent_crew.memory_hybrid.configured_write_embedder",
                        lambda: embedder)
    result = type("Result", (), {"status": "completed", "summary": "done",
                                  "verdict": None, "pr_number": None})()
    capture_result_best_effort(task_db, "live-result", result)
    with sqlite3.connect(path) as db:
        covered = db.execute("SELECT count(*) FROM adr001_memory m JOIN adr001_vec v "
                             "ON m.rowid=v.rowid WHERE v.model_id=?",
                             (embedder.model_id,)).fetchone()[0]
    assert covered >= 2
    assert len(embedder.calls) >= 2


def test_live_result_capture_bounds_slow_endpoint_and_keeps_rows(tmp_path, monkeypatch):
    class SlowHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            time.sleep(2)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"embeddings": [[1.0, 0.5]]}')
            except BrokenPipeError:
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        path = str(tmp_path / "memory.db")
        HybridMemoryStorage(path)
        task_db = str(tmp_path / "tasks.db")
        queue = TaskQueue(task_db)
        queue.enqueue(TaskRequest(task_id="slow-result", task_type="implement",
                                  description="capture", project="agent_crew", context={}))
        monkeypatch.setenv("AGENT_CREW_SHADOW_MEMORY_DB", path)
        monkeypatch.setenv("AGENT_CREW_MEMORY_BACKEND", "hybrid")
        monkeypatch.setenv("AGENT_CREW_MEMORY_EMBED_ENDPOINT",
                           f"http://127.0.0.1:{server.server_port}/embed")
        monkeypatch.setenv("AGENT_CREW_MEMORY_MODEL_ID", "slow-test")
        result = type("Result", (), {"status": "completed", "summary": "done",
                                      "verdict": None, "pr_number": None})()
        started = time.perf_counter()
        capture_result_best_effort(task_db, "slow-result", result)
        elapsed = time.perf_counter() - started
        assert elapsed < 1.5
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT count(*) FROM adr001_memory").fetchone()[0] >= 2
            assert db.execute("SELECT count(*) FROM adr001_vec").fetchone()[0] == 0
    finally:
        server.shutdown()
        server.server_close()


def test_shadow_batch_stops_embedding_after_first_failure(tmp_path):
    embedder = StubEmbedder(fail=True)
    store = HybridMemoryStorage(str(tmp_path / "memory.db"), embedder=embedder)
    records = [MemoryRecord("episodic", f"task:{i}", {"text": f"body {i}"},
                            MemoryScope(project="test")) for i in range(3)]
    store.put_many_shadow(records)
    assert len(embedder.calls) == 1
    assert len(_vectors(store.path)) == 3


def test_endpoint_is_not_called_on_retrieval_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_MEMORY_EMBED_ENDPOINT", "http://127.0.0.1:9999/embed")
    monkeypatch.setenv("AGENT_CREW_MEMORY_MODEL_ID", "slow-endpoint")
    path = str(tmp_path / "memory.db")
    store = HybridMemoryStorage(path)
    SQLiteMemoryStorage.existing(path).put(MemoryRecord(
        "episodic", "slow:one", {"text": "slow query"}, MemoryScope(project="test")))
    calls = []

    def slow_open(*args, **kwargs):
        calls.append(1)
        time.sleep(.35)
        raise TimeoutError("slow endpoint")

    with patch("agent_crew.memory_hybrid.urllib.request.urlopen", side_effect=slow_open):
        started = time.perf_counter()
        store.retrieve_ranked(MemoryScope(project="test"), "slow query",
                              "implementer", 10, 10000)
    assert not calls
    assert time.perf_counter() - started < .3


def test_backfill_cli_reports_uncovered_rows_and_stops_after_failures(
        tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "memory.db")
    store = HybridMemoryStorage(path)
    for index in range(6):
        _put(store, f"backfill:{index}", f"body {index}")
    embedder = StubEmbedder(fail=True)
    monkeypatch.setattr("agent_crew.memory_hybrid.configured_write_embedder",
                        lambda: embedder)
    monkeypatch.setattr(sys, "argv", ["backfill_memory_vectors.py", "--db", path])
    assert backfill_memory_vectors.main() != 0
    output = json.loads(capsys.readouterr().out)
    assert output["written"] == 0
    assert output["failed"] >= 3
    assert output["remaining"] == 6
    assert len(embedder.calls) == 3


def test_backfill_cli_reports_complete_coverage(tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "memory.db")
    store = HybridMemoryStorage(path)
    for index in range(2):
        _put(store, f"complete:{index}", f"body {index}")
    embedder = StubEmbedder()
    monkeypatch.setattr("agent_crew.memory_hybrid.configured_write_embedder",
                        lambda: embedder)
    monkeypatch.setattr(sys, "argv", ["backfill_memory_vectors.py", "--db", path])
    assert backfill_memory_vectors.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert (output["written"], output["failed"], output["remaining"]) == (2, 0, 0)
