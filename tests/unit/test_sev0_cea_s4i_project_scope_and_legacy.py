"""SEV-0 CEA step 4i — the post-admission gates read the *receipt's* project,
and a receipt-less row is REPORTed rather than only logged.

Contract: alfred ``sev0/e11-adr-draft`` @ ``6cbce565`` (Π P2, P3, P7, §7, §11).

**Item 1 — T3 call sites resolve the rollout mode from the receipt.**
Step 4c made rollout per-project but wired only ENQUEUE (which has the
``TaskRequest``) and the T5 artifact gate (which reads the task row). The four
post-admission points — claim, dispatch, execute_start, result — still asked
``TaskQueue.cea_config()`` with no argument and got the process-wide mode. s4c
recorded what that combination does: with ``AGENT_CREW_CEA_MODE=enforce`` and a
project held at ``shadow``, ``POST /result`` answered **409 from the s4b RESULT
nonce rule before the T5 artifact gate was reached** — the project was admitted
under one mode and finished under another. Half-enforced is the one state a
rollout must not have, so these tests pin both directions.

**Item 2 — receipt-less legacy rows.**
A row written before step 2c has no receipt, so the gate cannot be asked. Under
``enforce`` claim already refuses it (``queue.py`` ``claim_through_gate``).
Under ``shadow`` it runs, and the number a deployment needs before it turns
enforcement on is *how many are left* — which a log line cannot answer after the
fact. So each such row leaves a durable ``cea_legacy_row`` event and ``/health``
reports ``cea.legacy_rows``.

Provenance: written against agent_crew ``e26ae5e`` (sev0/cea-lineage, s4h).
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from agent_crew.cea.engine import ENFORCE, OFF, SHADOW, EngineConfig
from agent_crew.protocol import TaskResult
from agent_crew.queue import AdmissionRefused, TaskQueue

from tests.unit.test_sev0_cea_s2c_writer_callsites import WIRED, admitted, task

PROCESS = "AGENT_CREW_CEA_MODE"
HOT = "hotproject"          # pinned by its own variable
COLD = "coldproject"        # rides the process-wide default


def _queue(tmp_path, name="s4i.db") -> TaskQueue:
    """A queue with **no** pinned config, so the env decides — which is the
    whole question here. ``cea_providers`` is still pinned: a test must not
    read the live ``~/alfred/governance`` snapshot (s4g)."""
    return TaskQueue(str(tmp_path / name), cea_providers=dict(WIRED))


def _events(q: TaskQueue, task_id: str, event: str) -> list:
    conn = sqlite3.connect(q._db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [json.loads(r["fields"]) for r in conn.execute(
            "SELECT fields FROM task_exec_events WHERE task_id = ? AND event = ?"
            " ORDER BY event_id", (task_id, event)).fetchall()]
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# item 1 — the mode comes off the receipt
# ═══════════════════════════════════════════════════════════════════════════

def test_the_config_is_resolved_from_the_receipts_project(tmp_path, monkeypatch):
    monkeypatch.setenv(PROCESS, SHADOW)
    monkeypatch.setenv(f"AGENT_CREW_CEA_MODE__{HOT.upper()}", ENFORCE)
    q = _queue(tmp_path)
    assert q.cea_config_for_receipt({"project": HOT}).mode == ENFORCE
    assert q.cea_config_for_receipt({"project": COLD}).mode == SHADOW


@pytest.mark.parametrize("receipt", [None, {}, {"project": ""}, {"project": "   "}, "not-a-dict"])
def test_a_receipt_with_no_project_falls_back_to_the_process_wide_mode(
        tmp_path, monkeypatch, receipt):
    """There is no signed project to key an override off, so the process-wide
    setting is the honest answer — not a guess at which project this was."""
    monkeypatch.setenv(PROCESS, ENFORCE)
    q = _queue(tmp_path)
    assert q.cea_config_for_receipt(receipt).mode == ENFORCE


def test_a_pinned_config_still_wins_over_the_receipt(tmp_path, monkeypatch):
    """An operator or test that pinned the mode pinned it (s4c)."""
    monkeypatch.setenv(f"AGENT_CREW_CEA_MODE__{HOT.upper()}", ENFORCE)
    q = TaskQueue(str(tmp_path / "pinned.db"), cea_config=EngineConfig(mode=SHADOW),
                  cea_providers=dict(WIRED))
    assert q.cea_config_for_receipt({"project": HOT}).mode == SHADOW


def test_the_four_post_admission_sites_no_longer_read_the_process_wide_mode():
    """Static: the marker s4c left for this step was ``cea_config()`` with no
    argument at those sites. Each one now names the receipt it is deciding on.

    Static because the property is *where the code reads the mode from*: a fifth
    site added later that calls ``cea_config()`` would leave every behavioural
    test below passing and the invariant gone.
    """
    import inspect

    from agent_crew.queue import TaskQueue as Q
    for name, receipt in (("_cea_claim_gate", "receipt"),
                          ("record_dispatch", "receipt"),
                          ("start_execution", "receipt"),
                          ("submit_result", "_receipt"),
                          ("requeue_through_gate", "receipt")):
        body = inspect.getsource(getattr(Q, name))
        assert f"cea_config_for_receipt({receipt})" in body, name


def test_a_project_held_at_shadow_is_not_enforced_by_the_process_wide_mode(
        tmp_path, monkeypatch):
    """The s4c discovery, as a test.

    Process-wide ``enforce``; this project is held at ``shadow``. The result is
    posted with no nonce — which is exactly what the s4b RESULT rule refuses
    under enforcement (``NONCE_MISSING``) and exactly what ``shadow`` exists to
    record instead. Before this step it raised ``AdmissionRefused`` (409).
    """
    monkeypatch.setenv(PROCESS, ENFORCE)
    monkeypatch.setenv(f"AGENT_CREW_CEA_MODE__{COLD.upper()}", SHADOW)
    q = _queue(tmp_path)
    q.enqueue(task("t1", task_type="review", project=COLD, context=admitted()))
    assert q.dequeue(agent="codex", role="reviewer") is not None, "claim runs in shadow"
    q.record_dispatch("t1", channel="tmux_pane", agent="codex", target="%1")
    q.submit_result("t1", TaskResult(task_id="t1", status="completed", summary="done"))
    gate = q._last_cea_result_gate
    assert gate is not None and gate.enforced is False
    assert gate.proceed is True, gate.reason


def test_a_project_pinned_to_enforce_is_enforced_inside_a_shadow_process(
        tmp_path, monkeypatch):
    """The other direction, which is the one a rollout actually uses: the fleet
    is still in ``shadow`` and one project has moved. Its tasks must be enforced
    even though the process-wide switch has not moved."""
    monkeypatch.setenv(PROCESS, SHADOW)
    monkeypatch.setenv(f"AGENT_CREW_CEA_MODE__{HOT.upper()}", ENFORCE)
    q = _queue(tmp_path)
    q.enqueue(task("t1", task_type="review", project=HOT, context=admitted()))
    assert q.dequeue(agent="codex", role="reviewer") is not None
    q.record_dispatch("t1", channel="tmux_pane", agent="codex", target="%1")
    with pytest.raises(AdmissionRefused):
        q.submit_result("t1", TaskResult(task_id="t1", status="completed", summary="done"))


def test_two_projects_in_one_process_are_decided_separately(tmp_path, monkeypatch):
    """One queue, one process, two rollout modes — the thing a single
    process-wide switch cannot express."""
    monkeypatch.setenv(PROCESS, SHADOW)
    monkeypatch.setenv(f"AGENT_CREW_CEA_MODE__{HOT.upper()}", ENFORCE)
    q = _queue(tmp_path)
    for tid, project in (("hot-1", HOT), ("cold-1", COLD)):
        q.enqueue(task(tid, task_type="review", project=project, context=admitted()))
        assert q.dequeue(agent="codex", role="reviewer") is not None
        q.record_dispatch(tid, channel="tmux_pane", agent="codex", target="%1")
    q.submit_result("cold-1", TaskResult(task_id="cold-1", status="completed", summary="ok"))
    with pytest.raises(AdmissionRefused):
        q.submit_result("hot-1", TaskResult(task_id="hot-1", status="completed", summary="ok"))


# ═══════════════════════════════════════════════════════════════════════════
# item 2 — receipt-less legacy rows
# ═══════════════════════════════════════════════════════════════════════════

def _legacy_row(q: TaskQueue, task_id="legacy-1", status="pending", project="agent_crew"):
    """A row admitted before receipts existed (the step-2c trigger dropped for
    the insert, then restored) — see ``test_sev0_cea_i2_static_dynamic``."""
    from agent_crew.cea import store as receipt_store

    c = sqlite3.connect(q._db_path)
    try:
        c.execute("DROP TRIGGER IF EXISTS trg_tasks_receipt_id_required")
        c.execute("INSERT INTO tasks (task_id, task_type, description, branch, priority,"
                  " context, status, created_at, project, last_activity_at)"
                  " VALUES (?, 'implement', 'legacy work', 'main', 3, '{}', ?, ?, ?, 0)",
                  (task_id, status, 1.0, project))
        c.commit()
    finally:
        c.close()
    receipt_store.ensure_schema(sqlite3.connect(q._db_path))
    return task_id


def test_a_legacy_claim_under_shadow_leaves_one_durable_report(tmp_path, monkeypatch):
    monkeypatch.setenv(PROCESS, SHADOW)
    q = _queue(tmp_path)
    tid = _legacy_row(q)
    assert q.dequeue(agent="claude", role="implementer") is not None, "shadow claims it"
    events = _events(q, tid, TaskQueue.CEA_LEGACY_ROW_EVENT)
    assert [e["point"] for e in events] == ["claim"]
    assert events[0]["reason"] == "NO_RECEIPT"
    assert events[0]["mode"] == SHADOW and events[0].get("enforced") in (None, False)


def test_a_legacy_dispatch_is_reported_too(tmp_path, monkeypatch):
    """DISPATCH mints no nonce for a receipt-less row, so EXECUTE_START and
    RESULT will have nothing to check. That is worth a row of its own."""
    monkeypatch.setenv(PROCESS, SHADOW)
    q = _queue(tmp_path)
    tid = _legacy_row(q)
    q.dequeue(agent="claude", role="implementer")
    assert q.record_dispatch(tid, channel="tmux_pane", agent="claude", target="%1") is None
    assert [e["point"] for e in _events(q, tid, TaskQueue.CEA_LEGACY_ROW_EVENT)] == \
        ["claim", "dispatch"]


def test_the_report_line_says_what_enforce_would_have_done(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(PROCESS, SHADOW)
    q = _queue(tmp_path)
    tid = _legacy_row(q)
    with caplog.at_level("WARNING"):
        q.dequeue(agent="claude", role="implementer")
    lines = [r.getMessage() for r in caplog.records if "REPORT legacy-row" in r.getMessage()]
    assert len(lines) == 1, lines
    assert tid in lines[0] and "point=claim" in lines[0] and "enforced=False" in lines[0]


def test_off_reports_nothing(tmp_path, monkeypatch):
    """``off`` computes no verdict, so there is nothing to record — the rollout
    escape hatch stays an escape hatch."""
    monkeypatch.setenv(PROCESS, OFF)
    q = _queue(tmp_path)
    tid = _legacy_row(q)
    q.dequeue(agent="claude", role="implementer")
    assert _events(q, tid, TaskQueue.CEA_LEGACY_ROW_EVENT) == []


def test_enforce_still_refuses_the_claim(tmp_path, monkeypatch):
    """The refusal is unchanged by the report: "no receipt" is not "no
    objection" (P2). ``shadow`` counts; ``enforce`` stops."""
    monkeypatch.setenv(PROCESS, "test")
    q = _queue(tmp_path)
    tid = _legacy_row(q)
    assert q.dequeue(agent="claude", role="implementer") is None
    assert _events(q, tid, TaskQueue.CEA_LEGACY_ROW_EVENT) == []


def test_the_count_is_of_rows_not_of_sightings(tmp_path, monkeypatch):
    """``total`` is ground truth — including rows no call site has touched.
    ``reported`` counts distinct tasks per point, so a row claimed twice is one
    legacy row and not two."""
    monkeypatch.setenv(PROCESS, SHADOW)
    q = _queue(tmp_path)
    _legacy_row(q, "legacy-a")
    _legacy_row(q, "legacy-b")
    _legacy_row(q, "untouched", status="pending")
    q.enqueue(task("modern", task_type="review", context=admitted()))
    q.dequeue(agent="claude", role="implementer")      # legacy-a
    q.dequeue(agent="claude", role="implementer")      # legacy-b
    out = q.cea_legacy_rows()
    assert out["total"] == 3 and out["open"] == 3
    assert out["reported"] == {"claim": 2}
    assert out["event"] == TaskQueue.CEA_LEGACY_ROW_EVENT
    assert "error" not in out


def test_health_reports_the_legacy_row_count(tmp_path, monkeypatch):
    """The number rides on the endpoint an operator already polls (#248's
    lesson: a fact nobody polls is a fact nobody has)."""
    import types

    from fastapi.testclient import TestClient

    from agent_crew.cea import wiring as cea_wiring
    from agent_crew.server import create_app

    # ⛔Stub the wiring: startup's `install_from_env` reads the live
    #   ~/alfred/governance snapshot, and a harness must read no production
    #   input (s4g). The queue under test is built by startup, so this is the
    #   only seam that keeps it hermetic.
    monkeypatch.setattr(cea_wiring, "install_from_env", lambda *a, **k: types.SimpleNamespace(
        providers=dict(WIRED), authority=None, mode=SHADOW, statuses=()))
    monkeypatch.setenv(PROCESS, SHADOW)
    db = str(tmp_path / "health.db")
    q = TaskQueue(db, cea_providers=dict(WIRED))
    _legacy_row(q, "legacy-a")
    # `state["queue"]` is built by the startup handler, which TestClient runs
    # only inside its context manager.
    with TestClient(create_app(db_path=db, project="agent_crew")) as client:
        body = client.get("/health").json()
    assert body["cea"]["legacy_rows"]["total"] == 1, body["cea"]
    assert body["cea"]["legacy_rows"]["by_status"] == {"pending": 1}
    assert body["cea"]["legacy_rows"]["reported"] == {}


def test_a_held_result_keeps_the_nonce_the_result_gate_needs():
    """Found by item 1, and only findable by it.

    `no_artifact_result` rebuilds the result field by field and forgot
    `executor_binding`. Under enforcement that turned a held completion into
    409 `NONCE_MISSING` at the P2 RESULT gate — so the row was never written
    and the artifact finding that produced the hold was discarded with it.
    Invisible while RESULT read the process-wide mode, because the projects
    that enforce the T5 artifact gate were the only ones enforcing at all.
    """
    from agent_crew.pipeline import no_artifact_result

    binding = {"nonce": "n-1", "presenter": "claude"}
    held = no_artifact_result(
        TaskResult(task_id="t1", status="completed", summary="green",
                   executor_binding=dict(binding)), "dispatch base absent")
    assert held.status == "failed"
    assert held.executor_binding == binding, "a held result must still be submittable"
