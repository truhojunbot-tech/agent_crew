"""A newer crew CLI must not migrate a database served by an older build."""

import io
import json
import sqlite3

from click.testing import CliRunner

from agent_crew import cli


CLI_SHA = "a" * 40
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


def _health(monkeypatch, commit):
    monkeypatch.setattr("agent_crew.provenance.build", lambda: {"commit": CLI_SHA})
    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout: io.BytesIO(
        json.dumps({"build": {"commit": commit}}).encode()))


def test_mismatched_served_build_refuses_before_schema_migration(tmp_path, monkeypatch):
    db, _ = _project(tmp_path)
    _health(monkeypatch, SERVER_SHA)

    result = CliRunner().invoke(cli.crew, [
        "run", "implement a change", "--project", "sample", "--base", str(tmp_path)])

    assert result.exit_code != 0
    assert CLI_SHA in result.output and SERVER_SHA in result.output
    assert "HTTP API" in result.output
    assert _triggers(db) == []


def test_equal_build_allows_writable_queue(tmp_path, monkeypatch):
    db, _ = _project(tmp_path)
    _health(monkeypatch, CLI_SHA)

    cli._writable_queue(str(db))

    assert _triggers(db)


def test_unreachable_server_keeps_writable_queue_behavior(tmp_path, monkeypatch):
    from urllib.error import URLError

    db, _ = _project(tmp_path)
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda url, timeout: (_ for _ in ()).throw(URLError("down")))

    cli._writable_queue(str(db))

    assert _triggers(db)


def test_status_remains_read_only_on_build_mismatch(tmp_path, monkeypatch):
    db, _ = _project(tmp_path)
    _health(monkeypatch, SERVER_SHA)
    monkeypatch.setattr(cli, "_fetch_tasks_by_status", lambda port, status: [])

    result = CliRunner().invoke(cli.crew, ["status", "sample", "--base", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Project: sample" in result.output
    assert _triggers(db) == []


def test_expire_dry_run_remains_read_only_on_build_mismatch(tmp_path, monkeypatch):
    db, _ = _project(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE tasks (task_id TEXT, task_type TEXT, "
                     "status TEXT, last_activity_at REAL)")
    _health(monkeypatch, SERVER_SHA)

    result = CliRunner().invoke(cli.crew, [
        "task", "expire-stale", "--project", "sample", "--base", str(tmp_path),
        "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "No stale tasks found" in result.output
    assert _triggers(db) == []
