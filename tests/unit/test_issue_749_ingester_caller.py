"""Configured ADR-001 backfill callers (#749)."""
import json
import asyncio
import sqlite3
import subprocess
import threading
import time

import pytest

from agent_crew import memory_ingest
from agent_crew.memory_runtime import SQLiteMemoryStorage
from fastapi.testclient import TestClient
from tests.unit.test_issue_615_server_merge_gate import _submit_test, checker  # noqa: F401


def _git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def test_configured_ingester_refreshes_remote_and_is_idempotent(tmp_path):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "config", "user.name", "Test")
    _git(source, "remote", "add", "origin", str(remote))
    doc = source / "docs" / "context-economics.md"
    doc.parent.mkdir()
    doc.write_text("# First policy\n")
    _git(source, "add", ".")
    _git(source, "commit", "-qm", "first")
    _git(source, "push", "-q", "-u", "origin", "HEAD:main")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(remote), str(checkout))
    db = tmp_path / "memory.db"
    SQLiteMemoryStorage(str(db))
    config = tmp_path / "ingest.json"
    config.write_text(json.dumps({"memory_db": str(db), "task_dbs": [],
                                  "repos": {"truhojunbot-tech/quota-core": str(checkout)},
                                  "interval_seconds": 60}))
    doc.write_text("# Revised policy\n")
    _git(source, "commit", "-qam", "revised")
    _git(source, "push", "-q", "origin", "HEAD:main")
    revised_commit = _git(source, "rev-parse", "HEAD")
    assert memory_ingest.run_configured_ingest(str(config)) == (0, 1)
    assert memory_ingest.run_configured_ingest(str(config)) == (0, 0)
    with sqlite3.connect(db) as conn:
        keys = [row[0] for row in conn.execute(
            "SELECT key FROM adr001_memory WHERE key LIKE 'doc:%'")]
    assert keys == [f"doc:truhojunbot-tech/quota-core:docs/context-economics.md@{revised_commit}"]


def test_bad_task_db_does_not_block_document_pass(tmp_path, caplog):
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    doc = repo / "docs" / "context-economics.md"
    doc.parent.mkdir()
    doc.write_text("# Policy\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "first")
    db = tmp_path / "memory.db"
    SQLiteMemoryStorage(str(db))
    missing_task_db = tmp_path / "missing-tasks.db"
    good_task_db = tmp_path / "good-tasks.db"
    with sqlite3.connect(good_task_db) as conn:
        conn.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, task_type TEXT, "
                     "description TEXT, branch TEXT, context TEXT, status TEXT, "
                     "created_at REAL, project TEXT, summary TEXT, verdict TEXT, "
                     "findings TEXT, pr_number INTEGER)")
        conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("review-1", "review", "approved", "main", "{}", "completed",
                      1, "agent_crew", "approved", "approve", "[]", None))
    config = tmp_path / "ingest.json"
    config.write_text(json.dumps({"memory_db": str(db),
                                  "task_dbs": [str(missing_task_db), str(good_task_db)],
                                  "repos": {"truhojunbot-tech/quota-core": str(repo)}}))

    assert memory_ingest.run_configured_ingest(str(config)) == (1, 1)
    assert str(missing_task_db) in caplog.text
    assert not missing_task_db.exists()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM adr001_memory WHERE key LIKE 'doc:%'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM adr001_memory WHERE key LIKE 'task_outcome:%'").fetchone()[0] == 1


def test_ingester_lock_skips_overlapping_run(tmp_path):
    SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    config = tmp_path / "ingest.json"
    config.write_text(json.dumps({"memory_db": str(tmp_path / "memory.db"),
                                  "task_dbs": [], "repos": {}}))
    with memory_ingest.ingest_lock(str(tmp_path / "memory.db") + ".ingest.lock"):
        assert memory_ingest.run_configured_ingest(str(config)) is None
    assert memory_ingest.run_configured_ingest(str(config)) == (0, 0)


@pytest.mark.parametrize("bad_kind", ["missing", "fetch"])
def test_bad_repo_does_not_block_later_valid_repo(tmp_path, caplog, bad_kind):
    good = tmp_path / "good"
    good.mkdir()
    _git(good, "init", "-q")
    _git(good, "config", "user.email", "test@example.com")
    _git(good, "config", "user.name", "Test")
    doc = good / "docs" / "context-economics.md"
    doc.parent.mkdir()
    doc.write_text("# Policy\n")
    _git(good, "add", ".")
    _git(good, "commit", "-qm", "first")
    db = tmp_path / "memory.db"
    SQLiteMemoryStorage(str(db))
    bad = tmp_path / "missing"
    if bad_kind == "fetch":
        bad.mkdir()
        _git(bad, "init", "-q")
        _git(bad, "remote", "add", "origin", str(tmp_path / "no-remote.git"))
    config = tmp_path / "ingest.json"
    config.write_text(json.dumps({"memory_db": str(db), "task_dbs": [],
                                  "repos": {"bad/repo": str(bad),
                                            "truhojunbot-tech/quota-core": str(good)}}))
    assert memory_ingest.run_configured_ingest(str(config)) == (0, 1)
    assert "bad/repo" in caplog.text
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM adr001_memory WHERE key LIKE 'doc:%'").fetchone()[0] == 1


def test_nonexistent_memory_database_is_not_created(tmp_path):
    missing = tmp_path / "typo" / "memory.db"
    config = tmp_path / "ingest.json"
    config.write_text(json.dumps({"memory_db": str(missing), "task_dbs": [], "repos": {}}))
    with pytest.raises(FileNotFoundError):
        memory_ingest.run_configured_ingest(str(config))
    assert not missing.exists()
    assert not missing.parent.exists()


@pytest.mark.parametrize("change", [
    {"memory_db": ""}, {"memory_db": None}, {"task_dbs": "bad"},
    {"task_dbs": [12]}, {"repos": []}, {"repos": {"repo": 12}},
    {"interval_seconds": 59}, {"interval_seconds": 3601},
    {"interval_seconds": True},
])
def test_load_config_rejects_invalid_fields(tmp_path, change):
    config = tmp_path / "ingest.json"
    config.write_text(json.dumps({"memory_db": "/tmp/memory.db", "task_dbs": [],
                                  "repos": {}, **change}))
    with pytest.raises(ValueError):
        memory_ingest.load_config(str(config))


def test_best_effort_logs_and_swallows_failure(monkeypatch, caplog):
    def fail(path):
        raise RuntimeError("ingest failure")
    monkeypatch.setattr(memory_ingest, "run_configured_ingest", fail)
    memory_ingest._run_best_effort("config.json")
    assert "ingest failure" in caplog.text


def test_lifespan_starts_schedule_and_rejects_invalid_config(tmp_path, monkeypatch, caplog):
    from agent_crew.server import create_app
    db = tmp_path / "memory.db"
    SQLiteMemoryStorage(str(db))
    config = tmp_path / "ingest.json"
    config.write_text(json.dumps({"memory_db": str(db), "task_dbs": [], "repos": {},
                                  "interval_seconds": 60}))
    started = threading.Event()
    calls = []
    async def periodic(path, interval):
        calls.append((path, interval))
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(memory_ingest, "periodic_ingest", periodic)
    monkeypatch.setenv("AGENT_CREW_MEMORY_INGEST_CONFIG", str(config))
    with TestClient(create_app(str(tmp_path / "tasks.db"), project="agent_crew",
                               watchdog_disabled=True, anomaly_disabled=True)):
        assert started.wait(2)
    assert calls == [(str(config), 60)]

    started.clear()
    config.write_text(json.dumps({"memory_db": str(db), "task_dbs": [], "repos": {},
                                  "interval_seconds": 5}))
    with TestClient(create_app(str(tmp_path / "tasks-invalid.db"), project="agent_crew",
                               watchdog_disabled=True, anomaly_disabled=True)):
        time.sleep(.02)
    assert not started.is_set()
    assert "memory ingest schedule configuration invalid" in caplog.text


def test_periodic_and_postmerge_callers(monkeypatch):
    calls = []
    monkeypatch.setattr(memory_ingest, "run_configured_ingest", lambda path: calls.append(path))
    async def one_tick():
        task = asyncio.create_task(memory_ingest.periodic_ingest("configured.json", 60))
        while not calls:
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(one_tick())
    worker = memory_ingest.launch_postmerge_ingest("configured.json")
    worker.join(timeout=2)
    assert calls == ["configured.json", "configured.json"]


def test_server_successful_merge_starts_ingester(tmp_path, monkeypatch, checker):
    config = tmp_path / "ingest.json"
    config.write_text(json.dumps({"memory_db": str(tmp_path / "memory.db"),
                                  "task_dbs": [], "repos": {}}))
    monkeypatch.setenv("AGENT_CREW_MEMORY_INGEST_CONFIG", str(config))
    monkeypatch.setenv("AGENT_CREW_AUTO_MERGE", "1")
    launches = []
    monkeypatch.setattr(memory_ingest, "launch_postmerge_ingest",
                        lambda path: launches.append(path))
    original_post = TestClient.post
    def post_with_project(client, url, *args, **kwargs):
        kwargs["headers"] = {**kwargs.get("headers", {}),
                             "X-Agent-Crew-Project": "agent_crew"}
        return original_post(client, url, *args, **kwargs)
    monkeypatch.setattr(TestClient, "post", post_with_project)
    _, calls = _submit_test(tmp_path, monkeypatch, title="add widget",
                            change_type="modify")
    assert "merge" in calls
    assert launches == [str(config)]


def test_server_already_merged_reconcile_starts_ingester(tmp_path, monkeypatch):
    config = tmp_path / "ingest.json"
    config.write_text(json.dumps({"memory_db": str(tmp_path / "memory.db"),
                                  "task_dbs": [], "repos": {}}))
    monkeypatch.setenv("AGENT_CREW_MEMORY_INGEST_CONFIG", str(config))
    monkeypatch.setenv("AGENT_CREW_AUTO_MERGE", "1")
    launches = []
    monkeypatch.setattr(memory_ingest, "launch_postmerge_ingest",
                        lambda path: launches.append(path))
    original_post = TestClient.post
    def post_with_project(client, url, *args, **kwargs):
        kwargs["headers"] = {**kwargs.get("headers", {}),
                             "X-Agent-Crew-Project": "agent_crew"}
        return original_post(client, url, *args, **kwargs)
    monkeypatch.setattr(TestClient, "post", post_with_project)
    _, calls = _submit_test(tmp_path, monkeypatch, title="add widget",
                            change_type="modify", pr_status="merged")
    assert "merge" not in calls
    assert launches == [str(config)]
