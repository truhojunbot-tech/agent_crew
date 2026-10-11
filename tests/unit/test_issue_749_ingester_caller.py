"""Configured ADR-001 backfill callers (#749)."""
import json
import asyncio
import sqlite3
import subprocess

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
    config = tmp_path / "ingest.json"
    config.write_text(json.dumps({"memory_db": str(db), "task_dbs": [],
                                  "repos": {"truhojunbot-tech/quota-core": str(checkout)},
                                  "interval_seconds": 60}))
    doc.write_text("# Revised policy\n")
    _git(source, "commit", "-qam", "revised")
    _git(source, "push", "-q", "origin", "HEAD:main")
    assert memory_ingest.run_configured_ingest(str(config)) == (0, 1)
    assert memory_ingest.run_configured_ingest(str(config)) == (0, 0)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM adr001_memory WHERE key LIKE 'doc:%'").fetchone()[0] == 1


def test_ingester_lock_skips_overlapping_run(tmp_path):
    config = tmp_path / "ingest.json"
    config.write_text(json.dumps({"memory_db": str(tmp_path / "memory.db"),
                                  "task_dbs": [], "repos": {}}))
    with memory_ingest.ingest_lock(str(tmp_path / "memory.db") + ".ingest.lock"):
        assert memory_ingest.run_configured_ingest(str(config)) is None
    assert memory_ingest.run_configured_ingest(str(config)) == (0, 0)


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
