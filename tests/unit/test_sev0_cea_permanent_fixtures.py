"""SEV-0 CEA step 4d — permanent counterexample fixtures on the CEA components (ADR §12.2).

Contract: alfred ``sev0/e11-adr-draft`` @ ``6cbce565`` (§12.2); findings from
alfred ``evidence/sev0-p0/E10-CLAUDE-REDTEAM.md`` @ ``6da9ce3`` (4a–4j) and the
Codex challenge ``sev0-e10-codex-challenge-r1`` (CXC-1..7), plus Π CX-P2b /
CX-P2c (same-uid executor / caller impersonation).

``test_sev0_cea_fixtures.py`` keeps the same findings as HTTP-level
``xfail(strict=True)`` red baselines against ``b574308``; this file asks the
question one layer down — **does the CEA component (engine, validator, store,
queue adapter) block the scenario today?** — and reports one of:

* ``BLOCKED`` — the component refuses, with the exact code it refused with;
* ``EXPECTED_RED`` — the ADR itself says this stays red until the P2a
  credential boundary (O3/O21/O21b) exists; the fixture asserts the *honest
  degraded* outcome (``UNVERIFIED`` recorded, no identity-dependent ALLOW)
  instead of hiding the gap;
* ``OPEN`` — not blocked yet. Only a named ``DEFERRED`` item may be non-BLOCKED,
  and it is strict-red (``xfail(strict=True)``); the report-gate tests fail on
  any undeclared OPEN/EXPECTED_RED and keep the acceptance verdict red while
  ``DEFERRED`` is non-empty (4d-r2).

Every scenario runs under ``mode=test`` (enforcing, embedded permitted), on a
fresh SQLite per test. No server, no tmux, no network, no live DB.

Provenance: written against agent_crew ``bd58092`` (sev0/cea-lineage-s4d).
"""
from __future__ import annotations

import dataclasses
import re
import sqlite3

import pytest

from agent_crew.cea import callsites, store as receipt_store
from agent_crew.cea.engine import EngineConfig, UnauthenticatedCaller
from agent_crew.cea.intent import CallerProvenance
from agent_crew.cea.providers import SignatureStatus
from agent_crew.cea.runtime_state import RuntimeState
from agent_crew.cea.validator import ValidationOutcome as O, ValidationPoint as VP
from agent_crew.queue import TaskQueue

from tests.unit.sev0_cea_acceptance_helpers import (
    SRC, AuthorityState, enqueue_and_read, queue_for, task)
from tests.unit.test_sev0_cea_engine import caller, engine, forged_caller, intent

ADMITTED = {"authority_decision_ids": ["T0-1234"], "repo": "example/agent_crew"}
ACTIVE = AuthorityState("active")
TEST_MODE = EngineConfig(mode="test")

BLOCKED, EXPECTED_RED, OPEN = "BLOCKED", "EXPECTED_RED", "OPEN"


def _q(tmp_path, state=ACTIVE, name="q.db", providers=True):
    if providers:
        return queue_for(tmp_path, state, name=name)
    return TaskQueue(str(tmp_path / name), cea_config=TEST_MODE)


def _refused(admitted, receipt):
    return (not admitted) or receipt["decision"] != "ALLOW"


def _code(receipt):
    r = receipt.get("reason") or {}
    return r.get("code") if isinstance(r, dict) else str(r)


def _minted(state="QUEUED", **eng_kw):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    receipt_store.ensure_schema(conn)
    eng = engine(**eng_kw)
    r = dict(eng.authorize(conn, intent("pf-1"), caller()).receipt, state=state)
    return eng, r, conn


# ═══════════════════════════════════════════════════════════════════════════
# scenarios — each returns (status, exact reason)
# ═══════════════════════════════════════════════════════════════════════════

def cx_4a(tmp_path):
    """Direct POST /tasks with no wired authority ⇒ no row."""
    admitted, r = enqueue_and_read(_q(tmp_path, providers=False), task("a"), ingress="http.tasks")
    return (BLOCKED if not admitted else OPEN), f"admitted={admitted} code={_code(r)}"


def cx_4b(tmp_path):
    """coordinator_managed is provenance only: identical decision with/without it."""
    out = []
    for i, cm in enumerate((False, True)):
        ctx = dict(ADMITTED, **({"coordinator_managed": True} if cm else {}))
        adm, r = enqueue_and_read(_q(tmp_path, name=f"b{i}.db"), task("b", context=ctx),
                                  ingress="http.tasks")
        out.append((adm, r["decision"], _code(r)))
    return (BLOCKED if out[0] == out[1] else OPEN), f"without/with cm: {out}"


def cx_4c(tmp_path):
    q = _q(tmp_path)
    enqueue_and_read(q, task("c1", context=ADMITTED, description="first wording"),
                     ingress="cron.watch")
    admitted, r = enqueue_and_read(
        q, task("c2", context=dict(ADMITTED, operation_id="op-NEW"),
                description="totally reworded"), ingress="cron.watch")
    return (BLOCKED if not admitted else OPEN), f"second admission admitted={admitted} code={_code(r)}"


def _drift(field_value):
    eng, r, _ = _minted("QUEUED")
    cur = callsites.current_inputs(eng, r)
    b = dict(cur.binding, **field_value)
    g = callsites.gate_claim(r, claimant=r["executor_binding"],
                             current=dataclasses.replace(cur, binding=b), config=TEST_MODE)
    return g


def cx_4d(tmp_path):
    """A newer in-scope decision after admission ⇒ generation moves ⇒ not PROCEED."""
    eng, r, _ = _minted("QUEUED")
    g = _drift({"policy_generation": (r["binding"]["policy_generation"] or 0) + 1})
    return (BLOCKED if not g.proceed else OPEN), f"{g.outcome.value}: {g.reason}"


def cx_4e(tmp_path):
    st = AuthorityState("badsig", snapshot_signature=SignatureStatus.INVALID)
    admitted, r = enqueue_and_read(_q(tmp_path, st), task("e", context=ADMITTED),
                                   ingress="http.tasks")
    return (BLOCKED if _refused(admitted, r) else OPEN), f"admitted={admitted} code={_code(r)}"


def cx_4f(tmp_path):
    ctx = dict(ADMITTED, authority_decision_ids=["status: looks fine to me"])
    admitted, r = enqueue_and_read(_q(tmp_path), task("f", context=ctx), ingress="http.tasks")
    return (BLOCKED if _refused(admitted, r) else OPEN), f"decision={r['decision']} code={_code(r)}"


def cx_4g(tmp_path):
    eng, r, _ = _minted("QUEUED")
    revs = [dict(d, body_hash="e" * 32) for d in r["binding"]["source_decision_revs"]]
    g = _drift({"source_decision_revs": revs})
    return (BLOCKED if not g.proceed else OPEN), f"{g.outcome.value}: {g.reason}"


def cx_4h(tmp_path):
    """An honestly self-identified non-executor presenting /result ⇒ refused."""
    eng, r, _ = _minted("RUNNING")
    r["dispatch_nonces"] = [{"nonce": "n", "attempt": r["attempt"], "issued_at": r["issued_at"],
                             "used_at": r["issued_at"]}]
    cur = dataclasses.replace(callsites.current_inputs(eng, r), presented_nonce="n",
                              presenter="attacker", nonce_attempt=r["attempt"], nonce_unused=False,
                              nonce_consumed_by=f"execute_start:{r['executor_binding']}")
    g = callsites.gate_result(r, nonce="n", presenter="attacker", current=cur, config=TEST_MODE)
    return (BLOCKED if not g.proceed else OPEN), f"{g.outcome.value}: {g.reason}"


def cx_4i(tmp_path):
    """Free-text approver / reuse claims buy nothing: same decision with and without."""
    out = []
    for i, extra in enumerate(({}, {"approved_by": "me", "reuse": "REUSE_REVIEWED"})):
        adm, r = enqueue_and_read(_q(tmp_path, name=f"i{i}.db"),
                                  task("i", context=dict(ADMITTED, **extra)),
                                  ingress="http.tasks")
        out.append((adm, r["decision"], _code(r)))
    return (BLOCKED if out[0] == out[1] else OPEN), f"plain/claimed (must be identical): {out}"


def cx_4j(tmp_path):
    st = AuthorityState("q", runtime=RuntimeState.QUARANTINED)
    admitted, r = enqueue_and_read(_q(tmp_path, st), task("j", context=ADMITTED),
                                   ingress="http.tasks")
    eng, rc, _ = _minted("QUEUED")
    cur = callsites.current_inputs(eng, rc)
    b = dict(cur.binding, runtime_state="QUARANTINED")
    b["runtime_state_epoch"] = (b.get("runtime_state_epoch") or 0) + 1
    claim = callsites.gate_claim(rc, claimant=rc["executor_binding"],
                                 current=dataclasses.replace(cur, binding=b), config=TEST_MODE)
    rd = dict(rc, state="CLAIMED")
    disp = callsites.gate_dispatch(rd, attempt=rd["attempt"],
                                   current=dataclasses.replace(cur, binding=b), config=TEST_MODE)
    ok = (not admitted) and not claim.proceed and not disp.proceed
    return (BLOCKED if ok else OPEN), (f"enqueue admitted={admitted} code={_code(r)}; "
                                       f"claim={claim.reason}; dispatch={disp.reason}")


def cxc_1(tmp_path):
    """Exactly one capability matcher and no ack ledger in agent_crew src."""
    matchers = [f"{p.relative_to(SRC)}:{b[:m.start()].count(chr(10)) + 1}"
                for p in SRC.rglob("*.py") for b in [p.read_text(encoding="utf-8")]
                for m in re.finditer(r"^def (match_capabilities|admission_decision|reuse_preflight)\b",
                                     b, re.M)]
    acks = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
            if re.search(r"policy_ack\.json|ssot_decision_ack\.json", p.read_text(encoding="utf-8"))]
    ok = not matchers and not acks
    return (BLOCKED if ok else OPEN), (f"extra matchers={matchers} ack ledgers={acks}; "
                                       f"engine consumes E4 lookup only (engine._matched)")


def cxc_2(tmp_path):
    st = AuthorityState("reg-down", registry_available=False)
    admitted, r = enqueue_and_read(
        _q(tmp_path, st),
        task("x2", context=ADMITTED,
             description="Implement risk tier enforcement for Tokenomics dispatch"),
        ingress="cron.watch")
    return (BLOCKED if _refused(admitted, r) else OPEN), f"admitted={admitted} code={_code(r)}"


def cxc_3(tmp_path):
    ctx = {"coordinator_managed": True, "capability": "agent-crew.risk-tier-classifier"}
    admitted, r = enqueue_and_read(_q(tmp_path, providers=False), task("x3", context=ctx),
                                   ingress="http.tasks")
    return (BLOCKED if not admitted else OPEN), f"admitted={admitted} code={_code(r)}"


def cxc_4(tmp_path):
    conn = sqlite3.connect(":memory:")
    receipt_store.ensure_schema(conn)
    eng = engine()
    raised = []
    for c in (None, forged_caller()):
        try:
            eng.authorize(conn, intent("x4"), c)
        except UnauthenticatedCaller as exc:
            raised.append(type(exc).__name__)
    return (BLOCKED if len(raised) == 2 else OPEN), f"refusals={raised} (None, forged Caller)"


def cxc_5(tmp_path):
    src = (SRC.parents[1] / "tests" / "unit" / "test_sev0_e8_adversarial.py").read_text("utf-8")
    n = len(re.findall(r"@pytest\.mark\.xfail", src))
    return (BLOCKED if n == 0 else OPEN), f"E8 suite still carries {n} xfail markers at HTTP level"


def cxc_6(tmp_path):
    q = _q(tmp_path)
    enqueue_and_read(q, task("x6", context=ADMITTED), ingress="http.tasks")
    conn = sqlite3.connect(q._db_path)
    conn.execute("INSERT INTO task_exec_events (task_id, event, at, fields) "
                 "VALUES ('x6', 'dispatched', '2026-09-23T00:00:00Z', '{}')")
    conn.commit()
    res = {}
    for table, stmt in (("authorization_receipts", "UPDATE authorization_receipts SET state='X'"),
                        ("task_exec_events", "UPDATE task_exec_events SET event='rewritten'")):
        try:
            conn.execute(stmt)
            conn.commit()
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            res[table] = f"UPDATE accepted ({n} rows)"
        except sqlite3.DatabaseError as exc:
            res[table] = f"refused: {exc}"
    conn.close()
    ok = all(v.startswith("refused") for v in res.values())
    return (BLOCKED if ok else OPEN), str(res)


def cxc_7(tmp_path):
    from tests.unit import test_sev0_cea_i2_static_dynamic as i2
    i2.test_exactly_one_task_row_writer()
    i2.test_exactly_one_validator_instance()
    i2.test_call_sites_cover_every_validation_point_once()
    return BLOCKED, "covered by the I2 static test (one writer, one validator, five call sites)"


def cx_p2b(tmp_path):
    """Co-resident process holds the bound executor's nonce and presents as it."""
    eng, r, _ = _minted("RUNNING")
    r["dispatch_nonces"] = [{"nonce": "stolen", "attempt": r["attempt"],
                             "issued_at": r["issued_at"], "used_at": r["issued_at"]}]
    who = r["executor_binding"]
    cur = dataclasses.replace(callsites.current_inputs(eng, r), presented_nonce="stolen",
                              presenter=who, nonce_attempt=r["attempt"], nonce_unused=False,
                              nonce_consumed_by=f"execute_start:{who}")
    g = callsites.gate_result(r, nonce="stolen", presenter=who, current=cur, config=TEST_MODE)
    honest = (r["executor_binding_status"] == "UNVERIFIED" and r["decision"] != "ALLOW")
    if g.proceed and honest:
        return EXPECTED_RED, (f"accepted ({g.reason}); executor_binding_status=UNVERIFIED, "
                              f"decision={r['decision']} — no identity-dependent ALLOW (P2a); "
                              f"needs O3/O21 broker to become BLOCKED")
    if not g.proceed:
        return BLOCKED, f"{g.outcome.value}: {g.reason}"
    return OPEN, f"accepted and receipt not honest: {r['executor_binding_status']} {r['decision']}"


def cx_p2c(tmp_path):
    """Any same-uid process authenticates as the cron adapter and asks for admitted work."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    receipt_store.ensure_schema(conn)
    eng = engine()
    r = eng.authorize(conn, intent("p2c"), caller("impostor-pretending-cron",
                                                  CallerProvenance.CRON)).receipt
    honest = r["caller_identity_status"] == "UNVERIFIED" and r["decision"] != "ALLOW"
    if honest:
        return EXPECTED_RED, (f"caller_identity_status=UNVERIFIED, decision={r['decision']} "
                              f"({_code(r)}); caller_provenance recorded only; "
                              f"needs O21b to become 403 UNREGISTERED_CALLER")
    return OPEN, f"caller-class-dependent ALLOW under UNVERIFIED caller: {r['decision']}"


# ID -> (scenario, expected status today, reason if OPEN is expected)
FIXTURES = {
    "CX-4a": (cx_4a, BLOCKED), "CX-4b": (cx_4b, BLOCKED), "CX-4c": (cx_4c, BLOCKED),
    "CX-4d": (cx_4d, BLOCKED), "CX-4e": (cx_4e, BLOCKED), "CX-4f": (cx_4f, BLOCKED),
    "CX-4g": (cx_4g, BLOCKED), "CX-4h": (cx_4h, BLOCKED), "CX-4i": (cx_4i, BLOCKED),
    "CX-4j": (cx_4j, BLOCKED),
    "CXC-1": (cxc_1, BLOCKED), "CXC-2": (cxc_2, BLOCKED), "CXC-3": (cxc_3, BLOCKED),
    "CXC-4": (cxc_4, BLOCKED), "CXC-5": (cxc_5, BLOCKED), "CXC-6": (cxc_6, BLOCKED),
    "CXC-7": (cxc_7, BLOCKED),
    "CX-P2b": (cx_p2b, EXPECTED_RED), "CX-P2c": (cx_p2c, EXPECTED_RED),
}

#: 4d-r2 (Codex P1): no ``xfail(strict=False)``. A fixture is either PASS —
#: BLOCKED with its exact reason — or **strict-red**: asserted BLOCKED under
#: ``xfail(strict=True)`` naming the deferred item. A fix is then an XPASS that
#: fails the run until the entry is removed here; nothing is silently green.
#: EXPECTED_RED (honest UNVERIFIED, no identity-dependent ALLOW) is still not
#: BLOCKED, so the P2b/P2c fixtures are strict-red on the O21 broker too.
DEFERRED: dict[str, str] = {
    "CX-P2b": "O21c broker VERIFIED executor identity (P2b needs executor_binding_status="
              "VERIFIED to refuse a co-resident nonce holder)",
    "CX-P2c": "O21b/O21c broker VERIFIED caller identity (403 UNREGISTERED_CALLER)",
    "CXC-5": "E8 HTTP: test_sev0_e8_adversarial.py still carries xfail markers; flips when "
             "the server path enforces end-to-end",
    "CXC-6": "task_exec_events append-only trigger (authorization_receipts has one; "
             "UPDATE ... SET event='rewritten' succeeds on task_exec_events)",
}


def _params():
    for fid in FIXTURES:
        marks = ()
        if fid in DEFERRED:
            marks = (pytest.mark.xfail(strict=True, reason=f"{fid} deferred: {DEFERRED[fid]}"),)
        yield pytest.param(fid, id=fid, marks=marks)


@pytest.mark.parametrize("fid", list(_params()))
def test_permanent_fixture(tmp_path, fid, record_property):
    scenario, _ = FIXTURES[fid]
    status, reason = scenario(tmp_path)
    record_property("cea_fixture", f"{fid}: {status} — {reason}")
    print(f"\n[{fid}] {status}: {reason}")
    assert status == BLOCKED, f"{fid}: expected BLOCKED, got {status} — {reason}"


def _report(tmp_path) -> dict:
    out = {}
    for fid, (scenario, _) in FIXTURES.items():
        d = tmp_path / fid
        d.mkdir()
        out[fid] = scenario(d)
    return out


def test_report_gate_no_undeclared_open_result(tmp_path):
    """Hard gate: any fixture that is not BLOCKED must be a named DEFERRED item.
    A new OPEN (regression) or an undeclared EXPECTED_RED fails here, always."""
    rep = _report(tmp_path)
    bad = {f: r for f, r in rep.items() if r[0] != BLOCKED and f not in DEFERRED}
    assert not bad, bad


def test_report_gate_deferred_list_is_exactly_what_is_still_not_blocked(tmp_path):
    """A DEFERRED entry whose fixture now BLOCKs must be removed (it would
    otherwise keep a strict xfail around a passing fixture — caught as XPASS too)."""
    rep = _report(tmp_path)
    assert {f for f, r in rep.items() if r[0] != BLOCKED} == set(DEFERRED), rep


@pytest.mark.xfail(strict=True, reason="acceptance verdict is BLOCKED while DEFERRED is "
                   "non-empty: " + ", ".join(sorted(DEFERRED)))
def test_report_gate_acceptance_verdict_requires_every_fixture_blocked(tmp_path):
    rep = _report(tmp_path)
    assert all(r[0] == BLOCKED for r in rep.values()), {f: r for f, r in rep.items()
                                                        if r[0] != BLOCKED}


def test_every_e10_and_codex_finding_has_a_fixture():
    want = {f"CX-4{c}" for c in "abcdefghij"} | {f"CXC-{i}" for i in range(1, 8)} \
        | {"CX-P2b", "CX-P2c"}
    assert set(FIXTURES) == want
