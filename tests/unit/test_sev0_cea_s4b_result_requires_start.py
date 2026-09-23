"""SEV-0 CEA step 4b, FOLD-IN 3 — a dispatch nonce is not a bearer proof.

Contract: alfred ``sev0/e11-adr-draft`` @ ``6cbce565`` (Π P2's five points, P4).

Codex review of ``4d8538d`` (REQUEST_CHANGES, P1), reproduced verbatim:
``enqueue -> dequeue -> record_dispatch -> submit_result(nonce, presenter)``
with ``start_execution`` deliberately omitted was **accepted**. RESULT only
checked that the nonce appeared in the receipt's own ``dispatch_nonces`` array
— caller-controlled data — so the nonce worked straight at ``/result``: the
nonce table still held it unspent, the receipt never left ``CLAIMED``, and the
task reached a terminal result anyway. That is the fourth of P2's five points
bypassed.

The rule these tests pin: RESULT is valid only if the receipt is ``RUNNING``
for this attempt **and** the claim table says EXECUTE_START spent the nonce for
the same attempt. A result presented with an unspent nonce is refused
``NONCE_NOT_STARTED`` — over HTTP and over MCP alike.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from agent_crew.cea import store as receipt_store
from agent_crew.cea.engine import EngineConfig
from agent_crew.cea.validator import CurrentInputs, ValidationOutcome
from agent_crew.protocol import TaskResult
from agent_crew.queue import AdmissionRefused, TaskQueue

from tests.unit.test_sev0_cea_s2c_writer_callsites import WIRED, admitted, task

PANE = "%crew-s4b"


def _queue(path, *, mode="shadow") -> TaskQueue:
    return TaskQueue(str(path), cea_config=EngineConfig(mode=mode),
                     cea_providers=dict(WIRED))


def _row(db, task_id):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    finally:
        conn.close()


def _nonce_row(db, nonce):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return receipt_store.nonce_row(conn, nonce)
    finally:
        conn.close()


def _receipt_state(db, task_id):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        rid = conn.execute("SELECT receipt_id FROM tasks WHERE task_id = ?",
                           (task_id,)).fetchone()["receipt_id"]
        return (receipt_store.current_receipt(conn, rid) or {}).get("state")
    finally:
        conn.close()


def _dispatched(db, *, mode, task_id="t-start"):
    """enqueue → dequeue → record_dispatch. Stops one step short of /start."""
    q = _queue(db, mode=mode)
    q.enqueue(task(task_id, context=admitted()), ingress="http.tasks")
    got = q.dequeue(role="implementer", agent="claude")
    assert got is not None and got.task_id == task_id
    nonce = q.record_dispatch(task_id, channel="tmux_pane", agent="claude", target=PANE)
    assert nonce, "an admitted dispatch mints a nonce"
    return q, nonce


def _result(task_id="t-start"):
    return TaskResult(task_id=task_id, status="completed", summary="done")


# ═══════════════════════════════════════════════════════════════════════════
# the reproduction, and what it now does
# ═══════════════════════════════════════════════════════════════════════════

def test_result_without_execute_start_is_refused(tmp_path):
    """Codex's exact reproduction, in ``mode=test`` (enforcing).

    Before the fix this returned normally: ``nonce=True``, result accepted, the
    nonce still unspent and the receipt still ``CLAIMED``.
    """
    db = str(tmp_path / "repro.db")
    q, nonce = _dispatched(db, mode="test")

    assert _receipt_state(db, "t-start") == "CLAIMED"
    assert _nonce_row(db, nonce)["used_at"] is None, "nothing has spent it yet"

    with pytest.raises(AdmissionRefused) as exc:
        q.submit_result("t-start", _result(), nonce=nonce, presenter="claude")

    assert "NONCE_NOT_STARTED" in str(exc.value), str(exc.value)
    assert exc.value.point == "result"
    # and nothing was written: the refusal rolled the transaction back
    assert _row(db, "t-start")["status"] == "in_progress"
    assert _nonce_row(db, nonce)["used_at"] is None


def test_the_same_submission_is_accepted_after_start(tmp_path):
    """The one legitimate order still works, so the gate is not a wall."""
    db = str(tmp_path / "ok.db")
    q, nonce = _dispatched(db, mode="test")

    go = q.start_execution("t-start", nonce, presenter="claude")
    assert go["go"] is True and go["nonce_spent"] is True
    assert _receipt_state(db, "t-start") == "RUNNING"

    row = _nonce_row(db, nonce)
    assert row["used_at"] is not None
    assert row["used_by"] == f"{receipt_store.EXECUTE_START_CONSUMER}:claude", row

    q.submit_result("t-start", _result(), nonce=nonce, presenter="claude")

    assert _row(db, "t-start")["status"] == "completed"


def test_shadow_records_the_refusal_instead_of_hiding_it(tmp_path):
    """Under ``shadow`` the work proceeds and the answer is still the truth.

    The measurement shadow exists to produce is worthless if the gate reports
    PROCEED for a submission it would have refused.
    """
    db = str(tmp_path / "shadow.db")
    q, nonce = _dispatched(db, mode="shadow")

    q.submit_result("t-start", _result(), nonce=nonce, presenter="claude")

    gate = json.loads(_row(db, "t-start")["context"])["cea_result"]
    assert gate["outcome"] == "BLOCK"
    assert "NONCE_NOT_STARTED" in gate["reason"], gate
    assert gate["proceed"] is True and gate["enforced"] is False


def test_a_nonce_spent_by_something_other_than_start_does_not_count(tmp_path):
    """Spent is not the same fact as *started*.

    ``consume_nonce`` is a plain conditional UPDATE; if any future caller spends
    a nonce for its own reason, RESULT must still refuse. The claim table is
    asked *who*, not merely *whether*.
    """
    db = str(tmp_path / "other.db")
    q, nonce = _dispatched(db, mode="test")

    conn = sqlite3.connect(db)
    try:
        assert receipt_store.consume_nonce(conn, nonce, used_by="some_other_caller")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(AdmissionRefused) as exc:
        q.submit_result("t-start", _result(), nonce=nonce, presenter="claude")
    assert "NONCE_NOT_STARTED" in str(exc.value)


# ═══════════════════════════════════════════════════════════════════════════
# validator-level: the three facts it reads, and the None case
# ═══════════════════════════════════════════════════════════════════════════

def _running_receipt(tmp_path):
    """A RUNNING receipt straight from the store, for pure-validator cases."""
    db = str(tmp_path / "v.db")
    q, nonce = _dispatched(db, mode="test")
    q.start_execution("t-start", nonce, presenter="claude")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        rid = conn.execute("SELECT receipt_id FROM tasks WHERE task_id='t-start'").fetchone()["receipt_id"]
        return receipt_store.current_receipt(conn, rid)
    finally:
        conn.close()


@pytest.mark.parametrize("current,expected", [
    (dict(nonce_unused=None, nonce_consumed_by=None), "NONCE_NOT_STARTED"),
    (dict(nonce_unused=True, nonce_consumed_by=None), "NONCE_NOT_STARTED"),
    (dict(nonce_unused=False, nonce_consumed_by=None), "NONCE_NOT_STARTED"),
    (dict(nonce_unused=False, nonce_consumed_by="result:claude"), "NONCE_NOT_STARTED"),
    (dict(nonce_unused=False, nonce_consumed_by="execute_start:claude"), "OK"),
])
def test_the_validator_reads_the_claim_table_not_the_receipt(tmp_path, current, expected):
    """``None`` is not "probably started" — a fact nobody read is not a fact."""
    from agent_crew.cea import callsites

    receipt = _running_receipt(tmp_path)
    nonce = (receipt["dispatch_nonces"] or [{}])[0].get("nonce")
    out = callsites.VALIDATOR.validate_result(
        receipt, nonce=nonce, presenter=receipt.get("executor_binding"),
        current=CurrentInputs(binding=receipt.get("binding"),
                              nonce_attempt=receipt.get("attempt"),
                              signature_verification=None, **current))
    assert expected in out.reason, out.reason


def test_a_started_nonce_from_another_attempt_is_refused(tmp_path):
    """"Consumed by EXECUTE_START" still has to mean *this* attempt."""
    from agent_crew.cea import callsites

    receipt = _running_receipt(tmp_path)
    nonce = (receipt["dispatch_nonces"] or [{}])[0].get("nonce")
    out = callsites.VALIDATOR.validate_result(
        receipt, nonce=nonce, presenter=receipt.get("executor_binding"),
        current=CurrentInputs(binding=receipt.get("binding"),
                              nonce_unused=False,
                              nonce_consumed_by="execute_start:claude",
                              nonce_attempt=(receipt.get("attempt") or 1) + 1))
    assert out.outcome is ValidationOutcome.BLOCK
    assert "NONCE_WRONG_ATTEMPT" in out.reason, out.reason


# ═══════════════════════════════════════════════════════════════════════════
# transport regressions — both, or the gate is one poll away from gone
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def enforcing_queues(monkeypatch):
    """Every ``TaskQueue`` built in this test — including the server's own —
    runs ``mode=test`` with the wired providers.

    ``create_app`` takes no CEA arguments on purpose (the mode is a deployment
    fact, read from the environment), so the injection point for a test is the
    constructor.
    """
    original = TaskQueue.__init__

    def patched(self, db_path, **kw):
        kw.setdefault("cea_config", EngineConfig(mode="test"))
        kw.setdefault("cea_providers", dict(WIRED))
        original(self, db_path, **kw)

    monkeypatch.setattr(TaskQueue, "__init__", patched)
    return patched


def _post_task(api, task_id):
    body = {"task_id": task_id, "task_type": "implement", "description": "add a --json flag",
            "branch": "main", "priority": 3, "project": "agent_crew",
            "context": admitted()}
    r = api.post("/tasks", json=body)
    assert r.status_code in (200, 201), r.text
    return r


def test_http_result_before_start_is_refused(tmp_path, enforcing_queues):
    """``POST /tasks/{id}/result`` before ``POST /tasks/{id}/start`` → 409.

    The worker must learn *which* rule refused it; a 500 would read as a server
    fault and be retried with the same bypass.
    """
    from fastapi.testclient import TestClient
    from agent_crew.server import create_app

    db = str(tmp_path / "http.db")
    TaskQueue(db)
    app = create_app(db, pane_map={}, port=8105, watchdog_disabled=True,
                     anomaly_disabled=True)
    with TestClient(app) as api:
        _post_task(api, "t-http-start")
        handed = api.get("/tasks/next", params={"role": "implementer", "agent": "claude"}).json()
        nonce = handed["dispatch_nonce"]
        assert nonce

        refused = api.post("/tasks/t-http-start/result", json={
            "task_id": "t-http-start", "status": "completed", "summary": "done",
            "executor_binding": {"nonce": nonce, "presenter": "claude"}})

        assert refused.status_code == 409, refused.text
        assert "NONCE_NOT_STARTED" in refused.json()["detail"]
        assert _row(db, "t-http-start")["status"] == "in_progress"

        # the legitimate order, over the same transport
        go = api.post("/tasks/t-http-start/start",
                      json={"nonce": nonce, "presenter": "claude"}).json()
        assert go["go"] is True
        accepted = api.post("/tasks/t-http-start/result", json={
            "task_id": "t-http-start", "status": "completed", "summary": "done",
            "executor_binding": {"nonce": nonce, "presenter": "claude"}})
        assert accepted.status_code == 200, accepted.text
        assert _row(db, "t-http-start")["status"] == "completed"


def test_mcp_result_before_start_is_refused(tmp_path, enforcing_queues):
    """Same submission, same refusal, over MCP.

    ⛔Both transports or neither. A gate only one transport enforces is a gate
      an agent walks around by changing how it polls.
    """
    from agent_crew import mcp_server

    db = str(tmp_path / "mcp.db")
    q = TaskQueue(db)
    q.enqueue(task("t-mcp-start", context=admitted()), ingress="http.tasks")
    got = q.dequeue(agent="claude", role="implementer", claimed_via="mcp")
    assert got is not None
    nonce = q.record_dispatch("t-mcp-start", channel="api", agent="claude",
                              target="mcp:claude")

    def _submit():
        result = TaskResult(task_id="t-mcp-start", status="completed", summary="done",
                            executor_binding={"nonce": nonce, "presenter": "claude"})
        seen_nonce, seen_presenter = result.take_executor_binding()
        try:
            TaskQueue(db).submit_result("t-mcp-start", result, nonce=seen_nonce,
                                        presenter=seen_presenter)
        except AdmissionRefused as exc:
            return {"acknowledged": False, "refused": exc.point, "error": str(exc)}
        return {"acknowledged": True}

    ack = _submit()
    assert ack["acknowledged"] is False, ack
    assert ack["refused"] == "result"
    assert "NONCE_NOT_STARTED" in ack["error"]
    assert _row(db, "t-mcp-start")["status"] == "in_progress"

    TaskQueue(db).start_execution("t-mcp-start", nonce, presenter="claude")
    assert _submit()["acknowledged"] is True
    assert _row(db, "t-mcp-start")["status"] == "completed"


def test_the_mcp_handler_itself_refuses_rather_than_raising():
    """Static: ``mcp_server.submit_result`` catches :class:`AdmissionRefused`.

    The behavioural test above exercises the queue through the same call the
    handler makes; this pins that the handler does not let the refusal escape
    as an unhandled exception on a transport with no status code.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src" / "agent_crew" / "mcp_server.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    handlers = [h for node in ast.walk(tree) if isinstance(node, ast.Try)
                for h in node.handlers
                if any(isinstance(n, ast.Attribute) and n.attr == "submit_result"
                       for n in ast.walk(node))]
    names = {h.type.id for h in handlers if isinstance(h.type, ast.Name)}
    assert "AdmissionRefused" in names, (
        "MCP submit_result does not handle AdmissionRefused; a refused P2 RESULT "
        "would surface as a transport error instead of a refusal")
