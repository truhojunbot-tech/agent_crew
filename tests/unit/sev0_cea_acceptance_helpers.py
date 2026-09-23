"""Shared fixture writers for the SEV-0 CEA step-4d acceptance suites.

Not a test module (no ``test_`` prefix). Everything here is a *fixture writer*:
it builds input-provider fakes and reads receipts back; it never asserts. The
three acceptance files (I1 property, I2 static/dynamic, permanent fixtures)
import from here so "change the fixture writer, not the scenarios" holds for all
three at once.

Contract: alfred ``sev0/e11-adr-draft`` @ ``6cbce565`` (Π P1–P7, §6, §7, §11.2,
§12). Receipt schema: ``tests/cea_contract/receipt.schema.json`` (byte-identical
to alfred ``sev0/cea-alfred-lineage`` @ ``e1063eb``).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from agent_crew.cea import store as receipt_store
from agent_crew.cea.engine import EngineConfig
from agent_crew.cea.providers import CapabilityLookup, PolicySnapshotRef, SignatureStatus
from agent_crew.cea.receipt import (
    BudgetClass, DecisionRev, HumanGate, HumanGateState, ProviderBudget, RegistryRef)
from agent_crew.cea.runtime_state import RuntimeState, RuntimeStateSnapshot
from agent_crew.protocol import TaskRequest
from agent_crew.queue import AdmissionRefused, TaskQueue

SRC = Path(__file__).resolve().parents[2] / "src" / "agent_crew"

DECISION = DecisionRev(decision_id="T0-1234", body_hash="b" * 32)


@dataclass(frozen=True)
class AuthorityState:
    """One point in the authority-state space I1 quantifies over (§12.1):
    policy generation/signature, registry availability, runtime state, budget,
    human gate. ``label`` is only for test ids."""
    label: str
    runtime: RuntimeState = RuntimeState.ACTIVE
    runtime_read_failed: bool = False
    snapshot_available: bool = True
    snapshot_signature: SignatureStatus = SignatureStatus.VALID
    generation: int = 7
    registry_available: bool = True
    registry_stale: bool = False
    budget: BudgetClass = BudgetClass.OK
    gate: HumanGateState = HumanGateState.NOT_REQUIRED
    extra: dict = field(default_factory=dict)


class _Snapshot:
    def __init__(self, s: AuthorityState):
        self.s = s

    def current(self, intent=None) -> PolicySnapshotRef:
        return PolicySnapshotRef(generation=self.s.generation, hash="h" * 16, produced_at=None,
                                 decisions=(DECISION,), in_scope=(DECISION,),
                                 signature=self.s.snapshot_signature,
                                 available=self.s.snapshot_available, tier="T0")


class _Registry:
    def __init__(self, s: AuthorityState):
        self.s = s

    def lookup(self, intent) -> CapabilityLookup:
        return CapabilityLookup(registry=RegistryRef(generation="2026-09-23.1", hash="r" * 16),
                                matches=(), anchor_matches=(),
                                available=self.s.registry_available, stale=self.s.registry_stale)


class _Runtime:
    def __init__(self, s: AuthorityState):
        self.s = s

    def current(self):
        return RuntimeStateSnapshot(state=self.s.runtime, epoch=3,
                                    read_failed=self.s.runtime_read_failed)


class _Budget:
    def __init__(self, s: AuthorityState):
        self.s = s

    def budget(self, provider):
        return ProviderBudget(provider=provider, state=self.s.budget, observed_at=None)


class _Gate:
    def __init__(self, s: AuthorityState):
        self.s = s

    def state(self, intent, snapshot):
        return HumanGate(self.s.gate)


def providers(state: AuthorityState) -> dict:
    return {"snapshots": _Snapshot(state), "capabilities": _Registry(state),
            "runtime": _Runtime(state), "budgets": _Budget(state), "gates": _Gate(state)}


def queue_for(tmp_path, state: AuthorityState, *, name: str, mode: str = "test") -> TaskQueue:
    return TaskQueue(str(tmp_path / name), cea_config=EngineConfig(mode=mode),
                     cea_providers=providers(state))


def task(task_id="t1", *, task_type="implement", context=None, project="agent_crew",
         description="add a --json flag", branch="main") -> TaskRequest:
    return TaskRequest(task_id=task_id, task_type=task_type, description=description,
                       branch=branch, priority=3, context=dict(context or {}), project=project)


def receipt_by_id(q: TaskQueue, receipt_id: str) -> Optional[dict]:
    conn = sqlite3.connect(q._db_path)
    conn.row_factory = sqlite3.Row
    try:
        return receipt_store.current_receipt(conn, receipt_id)
    finally:
        conn.close()


def receipt_for_task(q: TaskQueue, task_id: str) -> Optional[dict]:
    conn = sqlite3.connect(q._db_path)
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute("SELECT receipt_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return receipt_store.current_receipt(conn, r["receipt_id"]) if r else None
    finally:
        conn.close()


def enqueue_and_read(q: TaskQueue, req: TaskRequest, *, ingress: str):
    """Submit through one adapter; return ``(admitted: bool, receipt dict)``.

    A refused admission still has an audit receipt (P2) — that is the one read
    back, so the comparison is receipt-to-receipt on both branches.
    """
    try:
        q.enqueue(req, ingress=ingress)
    except AdmissionRefused as exc:
        return False, receipt_by_id(q, exc.receipt_id)
    return True, receipt_for_task(q, req.task_id)


# ═══════════════════════════════════════════════════════════════════════════
# transport-level harness (4d-r2: Codex P1 — "I1 does not run every real §7
# adapter/route"). Every real entry point constructs its own ``TaskQueue(db)``;
# ``inject_cea`` makes *every* construction in the process carry the chosen
# mode and fixture providers, and ``EnqueueSpy`` records what each adapter
# actually handed to the one admission entry after its own decoding.
# ═══════════════════════════════════════════════════════════════════════════

class LiveState:
    """Mutable pointer to an ``AuthorityState`` — lets a test build a parent
    task under ACTIVE and then flip the authority state before the cascade."""

    def __init__(self, s: AuthorityState):
        self.s = s

    def __getattr__(self, name):
        return getattr(self.s, name)


def inject_cea(monkeypatch, live: LiveState, *, mode: str = "test") -> None:
    orig = TaskQueue.__init__

    def _init(self, db_path, *a, **kw):
        kw.setdefault("cea_config", EngineConfig(mode=mode))
        kw.setdefault("cea_providers", providers(live))
        orig(self, db_path, *a, **kw)

    monkeypatch.setattr(TaskQueue, "__init__", _init)


class EnqueueSpy:
    """Wraps ``TaskQueue.enqueue``; records ``(ingress, req, admitted, receipt_id, db)``."""

    def __init__(self, monkeypatch):
        self.calls: list = []
        orig = TaskQueue.enqueue
        spy = self

        def _enqueue(q, req, *a, **kw):
            try:
                out = orig(q, req, *a, **kw)
            except AdmissionRefused as exc:
                spy.calls.append((kw.get("ingress"), req, False, exc.receipt_id, q._db_path))
                raise
            conn = sqlite3.connect(q._db_path)
            try:
                r = conn.execute("SELECT receipt_id FROM tasks WHERE task_id = ?",
                                 (req.task_id,)).fetchone()
            finally:
                conn.close()
            spy.calls.append((kw.get("ingress"), req, True, r[0] if r else None, q._db_path))
            return out

        monkeypatch.setattr(TaskQueue, "enqueue", _enqueue)


def persisted_receipt(db_path: str, receipt_id: Optional[str]) -> Optional[dict]:
    if not receipt_id:
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return receipt_store.current_receipt(conn, receipt_id)
    finally:
        conn.close()
