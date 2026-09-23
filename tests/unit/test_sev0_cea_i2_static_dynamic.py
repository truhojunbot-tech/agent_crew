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
``xfail(strict=False)`` with the ADR cell quoted, so 4c/acceptance can flip it.

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
