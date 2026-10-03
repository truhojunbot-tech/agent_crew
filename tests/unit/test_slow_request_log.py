"""Slow request diagnostics expose route timing without request data."""

import logging
import re
import sqlite3
import threading
import time

from fastapi.testclient import TestClient

from agent_crew.server import create_app
from agent_crew.queue import RequestSqliteTiming, TaskQueue, request_sqlite_timing


def _app(tmp_path, monkeypatch, threshold, pause=lambda: None):
    monkeypatch.setenv("AGENT_CREW_SLOW_REQUEST_MS", str(threshold))
    app = create_app(str(tmp_path / "tasks.db"), pane_map={}, worktree_map={},
                     watchdog_disabled=True, anomaly_disabled=True)

    @app.post("/probe/{item_id}")
    def probe(item_id: str, payload: dict):
        pause()
        return {"ok": True}

    return app


def _warnings(caplog):
    return [record.message for record in caplog.records
            if record.name == "agent_crew.server" and record.levelno == logging.WARNING
            and "slow request" in record.message]


def test_slow_handler_logs_once_with_template_and_no_body(tmp_path, monkeypatch, caplog):
    app = _app(tmp_path, monkeypatch, 1, pause=lambda: time.sleep(0.02))
    with TestClient(app) as client, caplog.at_level(logging.WARNING, logger="agent_crew.server"):
        response = client.post("/probe/private-id", json={"secret": "never-log-me"})
    assert response.status_code == 200
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "method=POST path=/probe/{item_id}" in warnings[0]
    assert "status=200" in warnings[0]
    assert "total_ms=" in warnings[0]
    assert "lock_wait_ms=" in warnings[0]
    assert "lock_hold_ms=" in warnings[0]
    assert "private-id" not in warnings[0]
    assert "never-log-me" not in warnings[0]


def test_fast_handler_emits_no_warning(tmp_path, monkeypatch, caplog):
    app = _app(tmp_path, monkeypatch, 1000)
    with TestClient(app) as client, caplog.at_level(logging.WARNING, logger="agent_crew.server"):
        assert client.post("/probe/one", json={"secret": "x"}).status_code == 200
    assert _warnings(caplog) == []


def test_zero_disables_slow_request_log(tmp_path, monkeypatch, caplog):
    app = _app(tmp_path, monkeypatch, 0, pause=lambda: time.sleep(0.02))
    with TestClient(app) as client, caplog.at_level(logging.WARNING, logger="agent_crew.server"):
        assert client.post("/probe/one", json={"secret": "x"}).status_code == 200
    assert _warnings(caplog) == []


def test_begin_immediate_records_wait_and_hold(tmp_path):
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    blocker = sqlite3.connect(db, check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")
    timer = threading.Timer(0.05, blocker.commit)
    timing = RequestSqliteTiming()
    token = request_sqlite_timing.set(timing)
    timer.start()
    try:
        conn = queue._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            time.sleep(0.01)
            conn.execute("COMMIT")
        finally:
            conn.close()
    finally:
        timer.join()
        blocker.close()
        request_sqlite_timing.reset(token)
    assert timing.lock_wait_seconds >= 0.03
    assert timing.lock_hold_seconds >= 0.01


def test_http_write_lock_wait_appears_in_request_log(tmp_path, monkeypatch, caplog):
    app = _app(tmp_path, monkeypatch, 1)
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)

    @app.post("/write/{item_id}")
    def write(item_id: str):
        conn = queue._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            time.sleep(0.01)
            conn.execute("COMMIT")
        finally:
            conn.close()
        return {"ok": True}

    with TestClient(app) as client:
        blocker = sqlite3.connect(db, check_same_thread=False)
        blocker.execute("BEGIN IMMEDIATE")
        timer = threading.Timer(0.05, blocker.commit)
        timer.start()
        try:
            with caplog.at_level(logging.WARNING, logger="agent_crew.server"):
                assert client.post("/write/private-id").status_code == 200
        finally:
            timer.join()
            blocker.close()
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "path=/write/{item_id}" in warnings[0]
    assert float(re.search(r"lock_wait_ms=([\d.]+)", warnings[0]).group(1)) >= 30
    assert float(re.search(r"lock_hold_ms=([\d.]+)", warnings[0]).group(1)) >= 10
