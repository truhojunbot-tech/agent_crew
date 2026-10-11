"""A crew CLI may write only when its queue schema matches the server."""

import io
import json
import sqlite3

import click
import pytest
from click.testing import CliRunner
from unittest.mock import patch

from agent_crew import cli
from agent_crew.queue import QUEUE_SCHEMA_VERSION


SERVER_SHA = "b" * 40


def _project(tmp_path):
    project_dir = tmp_path / "sample"
    project_dir.mkdir()
    db = project_dir / "tasks.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE legacy_marker (id INTEGER)")
    state = {"project": "sample", "port": 19995, "db": str(db),
             "session": "sample", "agents": [], "pane_ids": []}
    (project_dir / "state.json").write_text(json.dumps(state))
    return db, state


def _triggers(db):
    with sqlite3.connect(db) as conn:
        return [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")]


def _health(monkeypatch, commit, schema_version):
    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout: io.BytesIO(
        json.dumps({"project": "sample", "schema_version": schema_version,
                    "build": {"commit": commit}}).encode()))


def test_newer_server_schema_refuses_before_schema_migration(tmp_path, monkeypatch):
    db, _ = _project(tmp_path)
    _health(monkeypatch, SERVER_SHA, QUEUE_SCHEMA_VERSION + 1)

    with patch("agent_crew.cli._port_listening", return_value=True):
        result = CliRunner().invoke(cli.crew, [
            "run", "implement a change", "--project", "sample", "--base", str(tmp_path)])

    assert result.exit_code != 0
    assert str(QUEUE_SCHEMA_VERSION + 1) in result.output
    assert str(QUEUE_SCHEMA_VERSION) in result.output
    assert "HTTP API" in result.output
    assert _triggers(db) == []


def test_different_build_same_schema_allows_writable_queue(tmp_path, monkeypatch):
    db, _ = _project(tmp_path)
    _health(monkeypatch, SERVER_SHA, QUEUE_SCHEMA_VERSION)

    cli._writable_queue(str(db))

    assert _triggers(db)


def test_missing_server_schema_refuses_before_schema_migration(tmp_path, monkeypatch):
    db, _ = _project(tmp_path)
    _health(monkeypatch, SERVER_SHA, None)

    with pytest.raises(click.ClickException, match="server schema unknown"):
        cli._writable_queue(str(db))
    assert _triggers(db) == []


def test_unreachable_server_keeps_writable_queue_behavior(tmp_path, monkeypatch):
    from urllib.error import URLError

    db, _ = _project(tmp_path)
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda url, timeout: (_ for _ in ()).throw(URLError("down")))

    cli._writable_queue(str(db))

    assert _triggers(db)


def test_malformed_health_keeps_legacy_queue_behavior(tmp_path, monkeypatch):
    db, _ = _project(tmp_path)
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda url, timeout: io.BytesIO(b"not-json"))

    cli._writable_queue(str(db))

    assert _triggers(db)


def test_corrupt_state_port_keeps_legacy_queue_behavior(tmp_path):
    db, state = _project(tmp_path)
    state["port"] = "bogus"
    (db.parent / "state.json").write_text(json.dumps(state))

    cli._writable_queue(str(db))

    assert _triggers(db)


def test_status_remains_read_only_on_schema_mismatch(tmp_path, monkeypatch):
    db, _ = _project(tmp_path)
    _health(monkeypatch, SERVER_SHA, QUEUE_SCHEMA_VERSION + 1)
    monkeypatch.setattr(cli, "_fetch_tasks_by_status", lambda port, status: [])

    result = CliRunner().invoke(cli.crew, ["status", "sample", "--base", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Project: sample" in result.output
    assert _triggers(db) == []


def test_expire_dry_run_remains_read_only_on_schema_mismatch(tmp_path, monkeypatch):
    db, _ = _project(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE tasks (task_id TEXT, task_type TEXT, "
                     "status TEXT, last_activity_at REAL)")
    _health(monkeypatch, SERVER_SHA, QUEUE_SCHEMA_VERSION + 1)

    result = CliRunner().invoke(cli.crew, [
        "task", "expire-stale", "--project", "sample", "--base", str(tmp_path),
        "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "No stale tasks found" in result.output
    assert _triggers(db) == []
