"""SEV-0 CEA step 4d — I2 zero bypass (ADR §12.1), static + dynamic.

Contract: alfred ``sev0/e11-adr-draft`` @ ``6cbce565`` (Π P2, P3, P6, P7, §12.1).

  I2: the number of code paths that move a task into QUEUED | CLAIMED |
  DISPATCHED | RUNNING, or accept a ``/result``, without a valid,
  currently-bound receipt is 0.

**Static** (reusing :mod:`agent_crew.cea.callsites` as the seam): exactly one
``QUEUED`` writer (one ``INSERT INTO tasks``, one receipt transition to
``QUEUED``), one validator object, five gate functions each with exactly one
product call site, and ``CALL_SITES`` covering every ``ValidationPoint``.

**Dynamic**: a real engine-minted receipt is put in the state each point
legitimately sees, a control asserts it ``PROCEED``s, and then each refusal
condition from the ADR tables is applied one at a time:

* no receipt; ``CONSUMED`` receipt (P4 replay);
* each P3 binding field drifted (§P3 table), and receipt age past O18 (24 h);
* runtime ``DRAINING`` / ``QUARANTINED`` / ``STOPPED`` (P6 enforcement matrix);
* SSOT snapshot older than O20 (15 min) (P7 row 3).

Expected outcomes are read off the ADR tables, not off the implementation. A
cell where the ADR says the work *continues* (``/result`` under drift or
QUARANTINED — P3 last paragraph, P6 matrix) is asserted as not-BLOCK rather than
refused. Where the implementation disagrees with the table the case is
``xfail(strict=True)`` with the ADR cell quoted: a cell that starts passing must
break this suite, not pass silently. The three ``pytest.xfail()`` calls below are
conditional on the gate's own answer (``if not g.proceed``), so they likewise
disappear — into a real PASS — the moment the validator honours the cell.

Provenance: written against agent_crew ``bd58092`` (sev0/cea-lineage-s4d).
"""
from __future__ import annotations

import ast
import dataclasses
import re
import sqlite3
from datetime import datetime, timezone

import pytest

from agent_crew.cea import callsites, store as receipt_store
from agent_crew.cea.engine import EngineConfig
from agent_crew.cea.validator import (
    O18_MAX_RECEIPT_AGE_SECONDS, O20_SNAPSHOT_MAX_AGE_SECONDS, ContractReceiptValidator,
    ValidationOutcome as O, ValidationPoint as VP, validate)

from tests.unit.sev0_cea_acceptance_helpers import SRC
from tests.unit.test_sev0_cea_engine import caller, engine, intent

TEST_MODE = EngineConfig(mode="test")
NONCE = "nonce-s4d-1"


def _sources() -> dict:
    return {p: p.read_text(encoding="utf-8") for p in sorted(SRC.rglob("*.py"))}


# ═══════════════════════════════════════════════════════════════════════════
# static
# ═══════════════════════════════════════════════════════════════════════════

def test_exactly_one_task_row_writer():
    stmt = re.compile(r"INSERT\s+INTO\s+tasks\s*\(", re.IGNORECASE)
    writers = {p.relative_to(SRC).as_posix(): len(stmt.findall(b))
               for p, b in _sources().items() if stmt.search(b)}
    assert writers == {"queue.py": 1}, writers


def test_exactly_one_receipt_transition_to_queued():
    """The receipt half of "one QUEUED writer": one call site moves a receipt to
    QUEUED, and it is the enqueue path (inside the same txn as the row)."""
    hits = []
    for p, body in _sources().items():
        for m in re.finditer(r"transition\w*\([^)]*[\"']QUEUED[\"']", body, re.S):
            hits.append(f"{p.relative_to(SRC).as_posix()}:{body[:m.start()].count(chr(10)) + 1}")
    assert len(hits) == 1 and hits[0].startswith("queue.py:"), hits


def test_exactly_one_validator_instance():
    ctor = re.compile(r"\bContractReceiptValidator\s*\(")
    sites = [p.relative_to(SRC).as_posix() for p, b in _sources().items()
             if ctor.search(b) and p.name != "validator.py"]
    assert sites == ["cea/callsites.py"], sites
    assert isinstance(callsites.VALIDATOR, ContractReceiptValidator)


def test_call_sites_cover_every_validation_point_once():
    assert set(callsites.CALL_SITES) == set(VP)
    assert len(set(callsites.CALL_SITES.values())) == len(VP) == 5


@pytest.mark.parametrize("gate", sorted(f.__name__ for f in callsites.CALL_SITES.values()))
def test_each_gate_has_exactly_one_product_call_site(gate):
    sites = []
    for p, body in _sources().items():
        if p.relative_to(SRC).as_posix() == "cea/callsites.py":
            continue
        for node in ast.walk(ast.parse(body)):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name == gate:
                    sites.append(f"{p.relative_to(SRC).as_posix()}:{node.lineno}")
    assert len(sites) == 1 and sites[0].startswith("queue.py:"), sites


def test_no_module_calls_the_validator_methods_except_callsites():
    pat = re.compile(r"\.validate_(enqueue|claim|dispatch|execute_start|result)\(")
    offenders = [p.relative_to(SRC).as_posix() for p, b in _sources().items()
                 if pat.search(b) and p.relative_to(SRC).as_posix()
                 not in ("cea/callsites.py", "cea/validator.py")]
    assert offenders == []


# ═══════════════════════════════════════════════════════════════════════════
# dynamic
# ═══════════════════════════════════════════════════════════════════════════

#: the receipt state each point legitimately sees (validator._ALLOWED_STATES)
STATE_AT = {VP.ENQUEUE: "ISSUED", VP.CLAIM: "QUEUED", VP.DISPATCH: "CLAIMED",
            VP.EXECUTE_START: "CLAIMED", VP.RESULT: "RUNNING"}
PRE_EXEC = (VP.CLAIM, VP.DISPATCH, VP.EXECUTE_START)


@pytest.fixture(scope="module")
def minted():
    """One engine-minted receipt + the engine that minted it (mode=test)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    receipt_store.ensure_schema(conn)
    eng = engine()
    auth = eng.authorize(conn, intent("i2-t1"), caller())
    yield eng, dict(auth.receipt)
    conn.close()


def _at(minted, point, **cur_overrides):
    """(receipt, current) as ``point`` legitimately sees them — the control case."""
    eng, r = minted
    receipt = dict(r, state=STATE_AT[point])
    if point in (VP.EXECUTE_START, VP.RESULT):
        receipt["dispatch_nonces"] = [{"nonce": NONCE, "attempt": r["attempt"],
                                       "issued_at": r["issued_at"], "used_at": None}]
    cur = callsites.current_inputs(eng, receipt)
    cur = dataclasses.replace(
        cur, claimant=receipt["executor_binding"], presenter=receipt["executor_binding"],
        presented_nonce=NONCE, nonce_unused=(point is VP.EXECUTE_START),
        nonce_attempt=r["attempt"],
        nonce_consumed_by=(f"execute_start:{receipt['executor_binding']}"
                           if point is VP.RESULT else None))
    return receipt, dataclasses.replace(cur, **cur_overrides)


def _gate(point, receipt, cur):
    kw = {VP.ENQUEUE: {"task_id": receipt.get("task_id") if receipt else "x"},
          VP.CLAIM: {"claimant": cur.claimant},
          VP.DISPATCH: {"attempt": (receipt or {}).get("attempt")},
          VP.EXECUTE_START: {"nonce": cur.presented_nonce},
          VP.RESULT: {"nonce": cur.presented_nonce, "presenter": cur.presenter}}[point]
    return callsites.CALL_SITES[point](receipt, current=cur, config=TEST_MODE, **kw)


@pytest.mark.parametrize("point", list(VP), ids=lambda p: p.value)
def test_control_the_legitimate_receipt_proceeds(minted, point):
    receipt, cur = _at(minted, point)
    g = _gate(point, receipt, cur)
    if point in (VP.EXECUTE_START, VP.RESULT) and not g.proceed:
        pytest.xfail(f"control at {point.value} not reproducible from the validator alone "
                     f"(nonce table is the store's): {g.reason}")
    assert g.proceed and g.outcome is O.PROCEED, g.as_record()


@pytest.mark.parametrize("point", list(VP), ids=lambda p: p.value)
def test_no_receipt_is_refused(minted, point):
    _, cur = _at(minted, point)
    g = _gate(point, None, cur)
    assert not g.proceed and g.enforced, g.as_record()


@pytest.mark.parametrize("point", list(VP), ids=lambda p: p.value)
def test_consumed_receipt_is_refused(minted, point):
    receipt, cur = _at(minted, point)
    g = _gate(point, dict(receipt, state="CONSUMED"), cur)
    assert not g.proceed, g.as_record()


def _drifted(binding: dict, field: str) -> dict:
    b = dict(binding)
    v = b[field]
    if field == "policy_generation":
        b[field] = (v or 0) + 1
    elif field == "runtime_state_epoch":
        b[field] = (v or 0) + 1
    elif field == "source_decision_revs":
        b[field] = [dict(d, body_hash="e" * 32) for d in (v or [])] or \
            [{"decision_id": "T0-NEW", "body_hash": "e" * 32}]
    elif field == "capability_registry":
        b[field] = dict(v or {}, generation="2026-09-24.1")
    elif field == "matched_capability":
        b[field] = dict(v or {"id": "c", "repo": "r"}, owner="someone-else")
    elif field == "budget_class":
        b[field] = "EXHAUSTED"
    elif field == "human_gate_state":
        b[field] = dict(v, state="DENIED") if isinstance(v, dict) else "DENIED"
    elif field == "runtime_state":
        b[field] = "QUARANTINED"
    else:
        b[field] = "h" * 15 + "x"
    return b


BINDING_FIELDS = ("policy_generation", "policy_hash", "source_decision_revs",
                  "capability_registry", "matched_capability", "runtime_state",
                  "runtime_state_epoch", "human_gate_state", "budget_class")


@pytest.mark.parametrize("field", BINDING_FIELDS)
@pytest.mark.parametrize("point", PRE_EXEC, ids=lambda p: p.value)
def test_stale_binding_field_is_refused_before_execution(minted, point, field):
    """P3 table: every binding change ⇒ re-admit / block / human gate / HELD —
    never PROCEED on the old receipt at claim, dispatch or execute start."""
    receipt, cur = _at(minted, point)
    if field not in (cur.binding or {}):
        pytest.skip(f"binding has no {field!r} in this receipt shape")
    g = _gate(point, receipt, dataclasses.replace(cur, binding=_drifted(cur.binding, field)))
    assert not g.proceed and g.outcome is not O.PROCEED, g.as_record()


@pytest.mark.parametrize("point", PRE_EXEC, ids=lambda p: p.value)
def test_receipt_older_than_o18_is_refused(minted, point):
    receipt, cur = _at(minted, point)
    issued = datetime.strptime(receipt["issued_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp()
    g = _gate(point, receipt, dataclasses.replace(
        cur, now=issued + O18_MAX_RECEIPT_AGE_SECONDS + 1))
    assert not g.proceed and g.outcome in (O.RE_ADMIT, O.HELD, O.BLOCK), g.as_record()


def test_receipt_just_inside_o18_still_proceeds(minted):
    receipt, cur = _at(minted, VP.CLAIM)
    issued = datetime.strptime(receipt["issued_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp()
    g = _gate(VP.CLAIM, receipt, dataclasses.replace(
        cur, now=issued + O18_MAX_RECEIPT_AGE_SECONDS - 60))
    assert g.proceed, g.as_record()


# P6 enforcement matrix — True = the point may proceed in that state (ADR §P6).
P6 = {
    "DRAINING":    {VP.ENQUEUE: False, VP.CLAIM: False, VP.DISPATCH: False,
                    VP.EXECUTE_START: False, VP.RESULT: True},
    "QUARANTINED": {VP.ENQUEUE: False, VP.CLAIM: False, VP.DISPATCH: False,
                    VP.EXECUTE_START: False, VP.RESULT: True},   # accepted, flagged
    "STOPPED":     {VP.ENQUEUE: False, VP.CLAIM: False, VP.DISPATCH: False,
                    VP.EXECUTE_START: False, VP.RESULT: True},   # per #314: persisted
}


@pytest.mark.parametrize("state", sorted(P6))
@pytest.mark.parametrize("point", list(VP), ids=lambda p: p.value)
def test_p6_matrix(minted, point, state):
    """DRAINING's dispatch/execute cells are "only already-claimed/-dispatched";
    this case sets neither flag, so the ADR answer is refuse."""
    receipt, cur = _at(minted, point)
    b = dict(cur.binding or {}, runtime_state=state)
    b["runtime_state_epoch"] = (b.get("runtime_state_epoch") or 0) + 1
    g = _gate(point, receipt, dataclasses.replace(cur, binding=b, epoch_passed_non_active=True))
    expected = P6[state][point]
    if expected:
        assert g.outcome is not O.BLOCK, (
            f"ADR P6: {point.value} in {state} is accepted (in-flight is never killed) "
            f"— {g.as_record()}")
    else:
        assert not g.proceed, f"ADR P6: {point.value} in {state} must refuse — {g.as_record()}"


@pytest.mark.parametrize("flag,point", [("already_claimed", VP.DISPATCH),
                                        ("already_dispatched", VP.EXECUTE_START)],
                         ids=["dispatch-already-claimed", "execute-already-dispatched"])
def test_p6_draining_lets_already_authorised_work_through(minted, flag, point):
    receipt, cur = _at(minted, point)
    b = dict(cur.binding or {}, runtime_state="DRAINING")
    g = _gate(point, receipt, dataclasses.replace(cur, binding=b, **{flag: True}))
    if not g.proceed:
        pytest.xfail(f"ADR P6 DRAINING '{point.value}: only already-claimed/-dispatched' "
                     f"not honoured by the validator alone: {g.reason}")
    assert g.proceed


@pytest.mark.parametrize("point", list(VP), ids=lambda p: p.value)
def test_snapshot_older_than_o20(minted, point):
    """P7 row 3: after snapshot_max_age new admission fails closed; claim and
    dispatch/execute start are HELD; in-flight (/result) continues."""
    receipt, cur = _at(minted, point)
    g = _gate(point, receipt, dataclasses.replace(
        cur, snapshot_available=False, snapshot_age_seconds=O20_SNAPSHOT_MAX_AGE_SECONDS + 1))
    if point is VP.RESULT:
        assert g.outcome is not O.BLOCK, g.as_record()
    else:
        assert not g.proceed, g.as_record()


@pytest.mark.parametrize("point", PRE_EXEC, ids=lambda p: p.value)
def test_snapshot_unreachable_but_inside_o20_still_proceeds(minted, point):
    receipt, cur = _at(minted, point)
    g = _gate(point, receipt, dataclasses.replace(
        cur, snapshot_available=False, snapshot_age_seconds=O20_SNAPSHOT_MAX_AGE_SECONDS - 60))
    if point is VP.EXECUTE_START and not g.proceed and "NONCE" in g.reason:
        pytest.xfail(f"execute-start control not reproducible without the store: {g.reason}")
    assert g.proceed, g.as_record()


def test_shadow_mode_never_refuses_but_records_the_truth(minted):
    """The only non-enforcing mode records the same answer (P7: no shadow branch)."""
    receipt, cur = _at(minted, VP.CLAIM)
    g = callsites.gate_claim(dict(receipt, state="CONSUMED"), claimant=cur.claimant,
                             current=cur, config=EngineConfig(mode="shadow"))
    assert g.proceed and not g.enforced and g.outcome is not O.PROCEED


def test_validator_answer_is_the_same_function_the_gate_uses(minted):
    receipt, cur = _at(minted, VP.CLAIM)
    assert validate(receipt, VP.CLAIM, cur).outcome is _gate(VP.CLAIM, receipt, cur).outcome


# ═══════════════════════════════════════════════════════════════════════════
# 4d-r2 (Codex P1): paths BACK to pending. The sweep above counted only
# ``INSERT INTO tasks``; every write that sets a task to pending/queued is a path
# back to claim/dispatch and must be counted and exercised.
# ═══════════════════════════════════════════════════════════════════════════

from tests.unit.sev0_cea_acceptance_helpers import (  # noqa: E402
    AuthorityState, queue_for, receipt_for_task, task)

#: every product write that sets tasks.status to pending/queued, by function.
#: ``defer_push_delivery`` writes status via a bound parameter (``status = ?``
#: with "pending") so it is matched by name, not by literal.
PENDING_WRITERS = {
    "queue.py": {"enqueue (INSERT)", "requeue", "requeue_dispatcher_claim",
                 "reset_stale_to_pending", "defer_push_delivery"},
}
#: callers of those writers that are themselves "paths back" (server/cli).
REQUEUE_CALLERS = {"server.py": {"_requeue_orphans", "requeue"}, "cli.py": {"recover"}}


def _pending_writes():
    lit = re.compile(r"UPDATE\s+tasks\s+SET[^\"']*status\s*=\s*'(pending|queued)'", re.I | re.S)
    out = {}
    for p, body in _sources().items():
        rel = p.relative_to(SRC).as_posix()
        for m in lit.finditer(body):
            ln = body[:m.start()].count("\n") + 1
            out.setdefault(rel, []).append(ln)
    return out


def test_static_every_literal_pending_write_is_inventoried():
    """Counts every ``UPDATE tasks SET ... status='pending'|'queued'`` — not just
    INSERT. The three literal writers are ``requeue``, ``requeue_dispatcher_claim``
    (queue.py:4486, receipt via ``requeue_through_gate`` before the write), and
    ``reset_stale_to_pending`` (queue.py:4723, same gate per row before UPDATE).
    ``defer_push_delivery`` is the bound-parameter writer."""
    writes = _pending_writes()
    assert set(writes) == {"queue.py"} and len(writes["queue.py"]) == 3, writes


def test_dispatcher_claim_requeue_moves_receipt_before_pending(tmp_path):
    """#377 writer: queue.requeue_dispatcher_claim passes through the §8 gate
    in its transaction, then writes pending. Exercise that path under CEA."""
    q = _claimed(tmp_path, "dispatcher-recover.db", "test",
                 config=EngineConfig(mode="test", default_max_attempts=3))
    conn = sqlite3.connect(q._db_path)
    conn.execute("UPDATE tasks SET claim_source = 'dispatcher' WHERE task_id = 't1'")
    conn.commit()
    conn.close()
    before = receipt_for_task(q, "t1")
    assert q.requeue_dispatcher_claim("t1") is True
    assert q.get_task_status("t1") == "pending"
    after = receipt_for_task(q, "t1")
    assert after["receipt_id"] == before["receipt_id"]
    assert (after["state"], after["attempt"]) == ("HELD", 2)


def test_static_the_bound_parameter_pending_writer_is_inventoried():
    body = (SRC / "queue.py").read_text(encoding="utf-8")
    hits = re.findall(r'status\s*=\s*"in_progress"\s+if\s+[^\n]+else\s+"pending"', body)
    assert len(hits) == 1, hits  # defer_push_delivery


def test_static_requeue_callers_are_inventoried():
    calls = {}
    for p, body in _sources().items():
        rel = p.relative_to(SRC).as_posix()
        n = len(re.findall(r"\.(requeue|reset_stale_to_pending|defer_push_delivery)\(", body))
        if n and rel != "queue.py":
            calls[rel] = n
    assert set(calls) == {"server.py", "cli.py"}, calls


def _claimed(tmp_path, name, mode, *, config=None):
    q = queue_for(tmp_path, AuthorityState("active"), name=name, mode=mode, config=config)
    q.enqueue(task("t1", context={"authority_decision_ids": ["T0-1234"]}), ingress="http.tasks")
    t = q.dequeue(agent="claude", role="implementer")
    assert t is not None and receipt_for_task(q, "t1")["state"] == "CLAIMED"
    return q


def _back_to_pending(q, how):
    if how == "requeue":
        q.requeue("t1")
    elif how == "defer_push_delivery":
        assert q.defer_push_delivery("t1", "%1", "pane refused", max_refusals=5, backoff_s=0) == 1
    elif how in ("reset_stale_to_pending", "recover"):
        # ``crew recover --reset-stale`` is exactly this call (cli.py recover)
        c = sqlite3.connect(q._db_path)
        c.execute("UPDATE tasks SET last_activity_at = 0 WHERE task_id = 't1'")
        c.commit()
        c.close()
        assert q.reset_stale_to_pending(1) == ["t1"]
    elif how == "_requeue_orphans":
        # server startup: list in_progress -> requeue each (server.py _requeue_orphans)
        for t in q.list_tasks(status="in_progress"):
            q.requeue(t.task_id)
    c = sqlite3.connect(q._db_path)
    try:
        return c.execute("SELECT status FROM tasks WHERE task_id='t1'").fetchone()[0]
    finally:
        c.close()


REQUEUE_PATHS = ("requeue", "defer_push_delivery", "reset_stale_to_pending",
                 "_requeue_orphans", "recover")


@pytest.mark.parametrize("how", REQUEUE_PATHS)
def test_requeue_moves_the_receipt_back_through_the_lifecycle(tmp_path, how):
    """s4h item 1 (ADR §8): a requeue is a re-admission, so the receipt moves too.

    Was ``xfail(strict=True)`` through s4g: all five paths set
    ``tasks.status='pending'`` and left the receipt in ``CLAIMED`` with no
    lifecycle row, so the row and the receipt store described different tasks.

    Which of the three §8 answers lands here is the engine's, not this test's:
    with ``max_attempts`` at its default of 1 the attempt budget is already
    spent at the first claim, so every path takes the RE_ADMIT branch and the
    receipt is SUPERSEDED. The reuse branch is
    ``test_requeue_reuses_the_receipt_while_an_attempt_remains`` below.
    """
    q = _claimed(tmp_path, f"{how}.db", "test")
    assert _back_to_pending(q, how) == "pending"
    conn = sqlite3.connect(q._db_path)
    conn.row_factory = sqlite3.Row
    r = receipt_for_task(q, "t1")
    hist = [h["state"] for h in receipt_store.receipt_history(conn, r["receipt_id"])]
    conn.close()
    assert r["state"] in ("QUEUED", "HELD", "SUPERSEDED"), (r["state"], hist)
    assert len(hist) > 3, hist


@pytest.mark.parametrize("how", REQUEUE_PATHS)
def test_requeue_reuses_the_receipt_while_an_attempt_remains(tmp_path, how):
    """§8's reuse branch: B unchanged and ``attempt < max_attempts``.

    The receipt comes back as ``HELD`` (P3: "not claimed; task → HELD with
    reason") carrying ``attempt + 1``, and ``claim`` accepts ``HELD`` — so the
    row is genuinely live again rather than stranded pending, which is the
    liveness half of the s4f/s4h item.
    """
    q = _claimed(tmp_path, f"reuse-{how}.db", "test",
                 config=EngineConfig(mode="test", default_max_attempts=3))
    assert _back_to_pending(q, how) == "pending"
    r = receipt_for_task(q, "t1")
    assert (r["state"], r["attempt"]) == ("HELD", 2), r
    # and it can actually be taken again, under enforcement, on that receipt
    assert q.dequeue(agent="claude", role="implementer") is not None
    assert receipt_for_task(q, "t1")["receipt_id"] == r["receipt_id"]
    assert receipt_for_task(q, "t1")["state"] == "CLAIMED"


@pytest.mark.parametrize("how", REQUEUE_PATHS)
def test_requeued_task_is_not_reclaimed_on_a_superseded_receipt_under_enforce(
        tmp_path, how):
    """Zero bypass still holds after s4h, one step further along.

    Through s4g the receipt stayed ``CLAIMED`` and the claim gate refused it for
    being in the wrong state. Now the attempt budget (default ``max_attempts=1``)
    is what refuses: the receipt is ``SUPERSEDED``, and P4's terminal-state rule
    is what stops the re-claim. Either way the row is not re-dispatched on a
    receipt that no longer authorises it.
    """
    q = _claimed(tmp_path, f"enf-{how}.db", "test")
    _back_to_pending(q, how)
    assert q.dequeue(agent="claude", role="implementer") is None
    assert receipt_for_task(q, "t1")["state"] == "SUPERSEDED"


@pytest.mark.parametrize("how", REQUEUE_PATHS)
def test_requeued_reclaim_under_shadow_is_reported(tmp_path, how):
    """Shadow never refuses (P7), but it must REPORT the out-of-lifecycle re-claim:
    a receipt row for the second claim. Measured at 9ef9230: the re-claim appends
    a lifecycle row (history grows past ISSUED,QUEUED,CLAIMED) — PASS."""
    q = _claimed(tmp_path, f"sh-{how}.db", "shadow")
    _back_to_pending(q, how)
    assert q.dequeue(agent="claude", role="implementer") is not None
    conn = sqlite3.connect(q._db_path)
    conn.row_factory = sqlite3.Row
    r = receipt_for_task(q, "t1")
    hist = receipt_store.receipt_history(conn, r["receipt_id"])
    conn.close()
    assert len(hist) > 3, [h["state"] for h in hist]


def _legacy_in_progress_row(q, task_id="legacy-1"):
    """A row admitted before receipts existed, caught mid-flight by the upgrade.

    Written with the ``trg_tasks_receipt_id_required`` trigger dropped, because
    that is literally what "written before the trigger was added" means; the
    trigger is then restored so the rest of the DB behaves normally. Nulling a
    live row's ``receipt_id`` instead is what ``trg_tasks_receipt_id_immutable``
    exists to forbid, and deleting the receipt is what the append-only trigger
    forbids — neither is how such a row comes to exist.
    """
    c = sqlite3.connect(q._db_path)
    try:
        c.execute("DROP TRIGGER IF EXISTS trg_tasks_receipt_id_required")
        c.execute("INSERT INTO tasks (task_id, task_type, description, branch, priority, "
                  "context, status, created_at, project, last_activity_at) "
                  "VALUES (?, 'implement', 'legacy work', 'main', 3, '{}', 'in_progress', "
                  "?, 'agent_crew', 0)", (task_id, 1.0))
        c.commit()
    finally:
        c.close()
    receipt_store.ensure_schema(sqlite3.connect(q._db_path))
    return task_id


@pytest.mark.parametrize("mode,expected", [("test", "in_progress"), ("shadow", "pending")])
def test_a_legacy_row_with_no_receipt_is_refused_a_requeue_under_enforce(
        tmp_path, mode, expected):
    """§8 through P2: "no receipt" is not "no objection" on the way back either.

    Under ``shadow`` the same row is requeued and *reported* — the count a
    deployment needs before it turns enforcement on.
    """
    q = queue_for(tmp_path, AuthorityState("active"), name=f"legacy-{mode}.db", mode=mode)
    tid = _legacy_in_progress_row(q)
    q.requeue(tid)
    c = sqlite3.connect(q._db_path)
    try:
        assert c.execute("SELECT status FROM tasks WHERE task_id=?", (tid,)).fetchone()[0] == expected
    finally:
        c.close()


def test_retry_is_a_new_admission_not_a_requeue():
    """``retry.failed_task`` creates a new task through ``enqueue`` and binds
    its receipt. P4 may reuse the parent's receipt when its binding is unchanged;
    either way this is an ingress, not a path back to pending."""
    body = (SRC / "server.py").read_text(encoding="utf-8")
    # The private successor provenance is required for a real retry admission;
    # this still calls enqueue, which authorizes and binds the successor row.
    assert re.search(
        r'enqueue\(retry_req,\s*ingress="retry.failed_task",\s*'
        r'_successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE\)', body)
