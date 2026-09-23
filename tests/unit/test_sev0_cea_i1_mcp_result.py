"""SEV-0 CEA step 4d-r3 — MCP ``submit_result`` end-to-end vs the HTTP baseline.

Contract: alfred ``sev0/e11-adr-draft`` @ ``6cbce565`` (Π P2 RESULT, §2.2, §12.1).

MCP exposes no task-creating tool, so it is not an I1 *ingress*; it is the
second transport of the P2 RESULT point. The flow driven here is the real one a
worker runs, and only transport objects are touched:

  MCP ``get_next_task`` (claim + dispatch nonce) → HTTP ``POST /tasks/{id}/start``
  → MCP ``submit_result(executor_binding={nonce, presenter})``

against the all-HTTP baseline (``GET /tasks/next`` → ``/start`` → ``/result``),
each in a fresh DB under ``mode=test`` with fixture providers. Asserted equal:
the persisted receipt's ``(decision, reason, intent_hash, state)``, the task's
terminal status, and whether the nonce was spent. Result-before-start must be
refused on both (``NONCE_NOT_STARTED``) and leave the row ``in_progress``.

Provenance: agent_crew ``sev0/cea-lineage-s4d`` after merging
``sev0/cea-lineage`` (s4e wiring, ``b6be8d2``).
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent_crew.cea import store as receipt_store
from agent_crew.cea.schema import validate_receipt

from tests.unit.sev0_cea_acceptance_helpers import (
    AuthorityState, LiveState, inject_cea, persisted_receipt, task)

AGENT = "claude"


def _app(db):
    from agent_crew.server import create_app
    return create_app(db_path=str(db), pane_map={}, port=0, watchdog_disabled=True,
                      anomaly_disabled=True)


def _mcp_tools(db):
    from agent_crew.mcp_server import build_mcp_server
    return {n: t.fn for n, t in build_mcp_server(str(db))._tool_manager._tools.items()}


def _seed(client):
    req = task("t-mcp", context={"authority_decision_ids": ["T0-1234"]})
    r = client.post("/tasks", json=__import__("dataclasses").asdict(req))
    assert r.status_code < 300, r.text


def _state(db, task_id="t-mcp"):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT status, receipt_id FROM tasks WHERE task_id = ?",
                           (task_id,)).fetchone()
        rcpt = receipt_store.current_receipt(conn, row["receipt_id"])
    finally:
        conn.close()
    assert rcpt is not None and not validate_receipt(rcpt), validate_receipt(rcpt or {})
    return row["status"], rcpt


def _nonce_used(db, nonce):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return receipt_store.nonce_row(conn, nonce)["used_at"] is not None
    finally:
        conn.close()


def _view(db, nonce):
    status, r = _state(db)
    return (status, r["decision"], json.dumps(r["reason"], sort_keys=True), r["intent_hash"],
            r.get("state"), _nonce_used(db, nonce))


def _result_body(nonce):
    return {"task_id": "t-mcp", "status": "completed", "summary": "done",
            "executor_binding": {"nonce": nonce, "presenter": AGENT}}


def _run_mcp(tmp_path, *, start: bool):
    db = tmp_path / "mcp.db"
    with TestClient(_app(db), raise_server_exceptions=False) as c:
        _seed(c)
        tools = _mcp_tools(db)
        got = tools["get_next_task"](agent=AGENT, role="implementer")
        assert got and got["task_id"] == "t-mcp" and got.get("dispatch_nonce"), got
        nonce = got["dispatch_nonce"]
        if start:
            go = c.post("/tasks/t-mcp/start", json={"nonce": nonce, "presenter": AGENT}).json()
            assert go.get("go") is True, go
        ack = tools["submit_result"](**_result_body(nonce))
    return db, nonce, ack


def _run_http(tmp_path, *, start: bool):
    db = tmp_path / "http.db"
    with TestClient(_app(db), raise_server_exceptions=False) as c:
        _seed(c)
        got = c.get("/tasks/next", params={"agent": AGENT, "role": "implementer"}).json()
        assert got and got["task_id"] == "t-mcp" and got.get("dispatch_nonce"), got
        nonce = got["dispatch_nonce"]
        if start:
            go = c.post("/tasks/t-mcp/start", json={"nonce": nonce, "presenter": AGENT}).json()
            assert go.get("go") is True, go
        resp = c.post("/tasks/t-mcp/result", json=_result_body(nonce))
    return db, nonce, resp


@pytest.fixture
def live(monkeypatch):
    s = LiveState(AuthorityState("active"))
    inject_cea(monkeypatch, s)
    return s


def test_mcp_result_after_start_is_accepted_and_equals_http(tmp_path, live):
    m_db, m_nonce, ack = _run_mcp(tmp_path, start=True)
    h_db, h_nonce, resp = _run_http(tmp_path, start=True)
    assert ack.get("acknowledged") is True, ack
    assert resp.status_code == 200, resp.text
    m, h = _view(m_db, m_nonce), _view(h_db, h_nonce)
    assert m == h, f"MCP vs HTTP RESULT diverged:\n mcp={m}\nhttp={h}"
    assert m[0] == "completed" and m[-1] is True


def test_mcp_result_before_start_is_refused_like_http(tmp_path, live):
    m_db, m_nonce, ack = _run_mcp(tmp_path, start=False)
    h_db, h_nonce, resp = _run_http(tmp_path, start=False)
    assert ack.get("acknowledged") is False and ack.get("refused") == "result", ack
    assert "NONCE_NOT_STARTED" in json.dumps(ack)
    assert resp.status_code == 409 and "NONCE_NOT_STARTED" in resp.text, resp.text
    m, h = _view(m_db, m_nonce), _view(h_db, h_nonce)
    assert m == h, f"MCP vs HTTP refusal diverged:\n mcp={m}\nhttp={h}"
    assert m[0] == "in_progress" and m[-1] is False, m
