"""SEV-0 CEA step 4d — I1 ingress equivalence (ADR §12.1), as a property test.

Contract: alfred ``sev0/e11-adr-draft`` @ ``6cbce565`` (Π P2a, §2.2, §7, §12.1).

  I1: for every pair of ingresses in §2.2, the same ``intent`` under the same
  authority state yields the same ``decision`` and ``reason``.

Two halves, because either alone proves nothing:

* **static exhaustiveness** — the set of adapters exercised below *is* the set
  of ingresses the product has. It is computed from the code (every
  ``enqueue(..., ingress=<const>)`` call site, plus the FastAPI route table),
  not from the registry, and compared with the registry. A new ingress that is
  not registered fails here; a registered ingress nothing calls fails here.
* **dynamic equivalence** — ``N`` generated ``(intent, authority state)``
  fixtures are pushed through **every** registered adapter (``queue.enqueue``
  with that adapter's ``ingress`` id — the one admission entry all fourteen
  share, §7.1) under ``mode=test`` (enforcing, embedded permitted), each in a
  fresh DB, and ``(decision, reason, intent_hash)`` plus admitted/refused must be
  identical across adapters. ``caller_*``, ``receipt_id``, ``issued_at`` are the
  documented exceptions (§12.1).

Provenance: written against agent_crew ``bd58092`` (sev0/cea-lineage-s4d).
"""
from __future__ import annotations

import ast
import json
import random
import re

import pytest

from agent_crew.cea import adapters
from agent_crew.cea.providers import SignatureStatus
from agent_crew.cea.receipt import BudgetClass, HumanGateState
from agent_crew.cea.runtime_state import RuntimeState
from agent_crew.cea.schema import validate_receipt

from tests.unit.sev0_cea_acceptance_helpers import (
    SRC, AuthorityState, enqueue_and_read, queue_for, task)

#: §7 modules the task names for the static sweep. ``mcp_server.py`` and
#: ``protocol.py`` are included on purpose: they must contribute *zero* ingress
#: call sites (adapters.py documents MCP as deliberately absent).
SWEPT = ("server.py", "mcp_server.py", "pipeline.py", "loop.py", "watch.py",
         "triage.py", "discussion.py", "protocol.py", "cli.py")

#: The adapter list as documented in the ADR §2.2 / adapters.INGRESSES docstring.
DOCUMENTED = {
    "http.tasks", "cli.enqueue", "cli.discuss",
    "loop.implement", "loop.review", "loop.test",
    "cascade.review", "cascade.test", "cascade.fix", "cascade.fallback",
    "retry.failed_task", "watchdog.stale_review", "cron.watch", "cron.triage",
}


def _ingress_call_sites() -> dict[str, list[str]]:
    """``ingress id -> ["file:line", ...]`` for every ``*.enqueue(..., ingress=<const>)``."""
    found: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        if path.parent.name == "cea":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "ingress" and isinstance(kw.value, ast.Constant):
                    found.setdefault(kw.value.value, []).append(
                        f"{path.relative_to(SRC)}:{node.lineno}")
    return found


# ═══════════════════════════════════════════════════════════════════════════
# static exhaustiveness
# ═══════════════════════════════════════════════════════════════════════════

def test_registry_equals_the_documented_adapter_list():
    assert {i.id for i in adapters.INGRESSES} == DOCUMENTED


def test_every_ingress_call_site_in_the_code_is_a_registered_adapter_and_vice_versa():
    sites = _ingress_call_sites()
    assert set(sites) == set(adapters.BY_ID), (
        f"code-only: {sorted(set(sites) - set(adapters.BY_ID))}; "
        f"registered-but-uncalled: {sorted(set(adapters.BY_ID) - set(sites))}; sites={sites}")


def test_the_swept_modules_carry_every_adapter_and_mcp_protocol_carry_none():
    sites = _ingress_call_sites()
    files = {s.split(":")[0] for v in sites.values() for s in v}
    assert files <= set(SWEPT), f"ingress call sites outside the §7 sweep: {files - set(SWEPT)}"
    assert "mcp_server.py" not in files and "protocol.py" not in files


def test_every_task_creating_route_is_the_http_tasks_adapter():
    """Route table half: the only FastAPI route that creates a task is ``POST
    /tasks`` and it enqueues as ``http.tasks``. Other routes may mutate state
    (claim/start/result) — those are I2's call sites, not ingresses."""
    src = (SRC / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    creating = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        routes = [d for d in fn.decorator_list if isinstance(d, ast.Call)
                  and isinstance(d.func, ast.Attribute)
                  and d.func.attr in ("post", "put", "patch")]
        if not routes:
            continue
        body = ast.get_source_segment(src, fn) or ""
        if re.search(r"\.enqueue\(", body):
            path = routes[0].args[0].value if routes[0].args and isinstance(
                routes[0].args[0], ast.Constant) else "?"
            creating.append((path, sorted(set(re.findall(r'ingress="([^"]+)"', body)))))
    assert ("/tasks", ["http.tasks"]) in creating, creating
    assert all(ids and set(ids) <= set(adapters.BY_ID) for _, ids in creating), creating


# ═══════════════════════════════════════════════════════════════════════════
# dynamic equivalence
# ═══════════════════════════════════════════════════════════════════════════

AUTHORITY_STATES = (
    AuthorityState("active"),
    AuthorityState("draining", runtime=RuntimeState.DRAINING),
    AuthorityState("quarantined", runtime=RuntimeState.QUARANTINED),
    AuthorityState("stopped", runtime=RuntimeState.STOPPED),
    AuthorityState("runtime-unreadable", runtime_read_failed=True),
    AuthorityState("registry-down", registry_available=False),
    AuthorityState("registry-stale", registry_stale=True),
    AuthorityState("snapshot-down", snapshot_available=False),
    AuthorityState("snapshot-badsig", snapshot_signature=SignatureStatus.INVALID),
    AuthorityState("budget-exhausted", budget=BudgetClass.EXHAUSTED),
    AuthorityState("gate-pending", gate=HumanGateState.PENDING),
)

TASK_TYPES = ("implement", "review", "test", "discuss")  # protocol.TaskRequest Literal
AUTHORITY_IDS = (["T0-1234"], [], ["T0-NOT-IN-SNAPSHOT"])

N = 24
SEED = 51  # SEV-0 issue number: reproducible, and nobody tuned it


def _fixtures():
    rng = random.Random(SEED)
    out = []
    for i in range(N):
        state = AUTHORITY_STATES[i % len(AUTHORITY_STATES)]   # every state at least twice
        ctx = {"repo": rng.choice(("example/agent_crew", "example/other")),
               "authority_decision_ids": rng.choice(AUTHORITY_IDS)}
        if rng.random() < 0.4:
            ctx["scope_anchors"] = rng.sample(
                ["src/agent_crew/server.py", "src/agent_crew/queue.py", "docs/x.md"], 2)
        if rng.random() < 0.3:
            ctx["capability_id"] = "tokenomics.work_class_gate"
        if rng.random() < 0.3:
            # caller-supplied provenance claims — P2a: recorded, never an input
            ctx["coordinator_managed"] = True
        out.append(pytest.param(state, rng.choice(TASK_TYPES), ctx,
                                id=f"f{i:02d}-{state.label}"))
    return out


def _decision_view(admitted: bool, receipt: dict) -> tuple:
    return (admitted, receipt["decision"], json.dumps(receipt["reason"], sort_keys=True),
            receipt["intent_hash"])


@pytest.mark.parametrize("state,task_type,ctx", _fixtures())
def test_i1_every_adapter_yields_the_same_decision(tmp_path, state, task_type, ctx):
    views = {}
    for n, ingress in enumerate(adapters.INGRESSES):
        q = queue_for(tmp_path, state, name=f"i{n}.db")
        req = task(f"t-{n}", task_type=task_type, context=dict(ctx),
                   description=f"reworded per adapter #{n}")  # description is not identity (P4)
        admitted, receipt = enqueue_and_read(q, req, ingress=ingress.id)
        assert receipt is not None, f"{ingress.id}: no receipt at all (P2 audit row missing)"
        assert not validate_receipt(receipt), \
            f"{ingress.id}: receipt fails the frozen schema: {validate_receipt(receipt)}"
        assert receipt["caller_provenance"] == ingress.provenance.value
        assert receipt["caller_identity_status"] == "UNVERIFIED"   # P2a, no O21b broker
        views[ingress.id] = _decision_view(admitted, receipt)
    distinct = set(views.values())
    assert len(distinct) == 1, (
        f"I1 violated under {state.label}/{task_type}: " +
        "; ".join(f"{k}={v}" for k, v in sorted(views.items())))


def test_the_fixture_set_is_not_degenerate(tmp_path):
    """A property that only ever sees BLOCK proves nothing about ALLOW. At least
    one fixture must be admitted and at least one refused."""
    outcomes = set()
    for p in _fixtures():
        state, task_type, ctx = p.values
        q = queue_for(tmp_path, state, name=f"{p.id}.db")
        admitted, _ = enqueue_and_read(q, task("t", task_type=task_type, context=ctx),
                                       ingress="http.tasks")
        outcomes.add(admitted)
    assert outcomes == {True, False}, outcomes
