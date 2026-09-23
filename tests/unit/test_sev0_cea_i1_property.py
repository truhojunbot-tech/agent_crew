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

* **transport equivalence** (4d-r2, Codex P1) — the half above calls
  ``TaskQueue.enqueue`` directly, which only shows the common entry ignores the
  ``ingress`` string. The ``TRANSPORTS`` block below drives each *real* entry
  point (FastAPI ``POST /tasks`` via TestClient, ``crew enqueue`` via CliRunner,
  ``loop.enqueue_*``, ``discussion.enqueue_panel_tasks``, the pipeline review
  cascade, the ``watch.run_cycle`` cron wrapper) with every ``TaskQueue`` built
  in-process carrying mode=test + fixture providers, spies the request each
  adapter actually handed to admission, and compares the PERSISTED receipt's
  ``(admitted, decision, reason, intent_hash)`` with the same request posted over
  HTTP into a fresh DB. The route table and the MCP tool registry are frozen
  against documented lists, so a new unlisted route/tool fails.

Provenance: written against agent_crew ``bd58092`` (sev0/cea-lineage-s4d);
transport half against ``9ef9230`` (s4d merged with sev0/cea-lineage ``caf5644``).
"""
from __future__ import annotations

import ast
import json
import random
import re
import sqlite3

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


# ═══════════════════════════════════════════════════════════════════════════
# transport equivalence (4d-r2)
# ═══════════════════════════════════════════════════════════════════════════

from fastapi.testclient import TestClient  # noqa: E402
from click.testing import CliRunner  # noqa: E402

from tests.unit.sev0_cea_acceptance_helpers import (  # noqa: E402
    EnqueueSpy, LiveState, dispatch_and_start, inject_cea, persisted_receipt,
    seed_finished_parent)

#: Every mutating FastAPI route, frozen. ``POST /tasks`` is the only one that is
#: an ingress (http.tasks); retry.failed_task and watchdog.stale_review are
#: server-internal callers, not routes. A new route fails here until it is
#: classified — ingress (add a transport below) or not.
DOCUMENTED_MUTATING_ROUTES = {
    ("DELETE", "/tasks/{task_id}"), ("POST", "/admin/replay-suppressed"),
    ("POST", "/gates"), ("POST", "/gates/{gate_id}/resolve"),
    ("POST", "/pane_map/reload"), ("POST", "/runtime/coordinator/handoff"),
    ("POST", "/tasks"), ("POST", "/tasks/expire-stale"),
    ("POST", "/tasks/{task_id}/checkpoint"), ("POST", "/tasks/{task_id}/result"),
    ("POST", "/tasks/{task_id}/start"),
}
INGRESS_ROUTES = {("POST", "/tasks")}

#: MCP exposes no task-creating tool (adapters.py: MCP deliberately absent);
#: ``submit_result`` reaches admission only through the cascade.* adapters.
DOCUMENTED_MCP_TOOLS = {"get_next_task", "get_next_discuss_task", "submit_result",
                        "bump_activity", "get_task", "list_pending", "cancel_task"}

#: ingress id -> the transport test below that drives it for real. Ids absent
#: here are the remaining sub-items (reported, not silently passed).
TRANSPORTS = {"http.tasks": "http", "cli.enqueue": "cli", "loop.implement": "loop",
              "cli.discuss": "discussion", "cascade.review": "cascade_review",
              "cron.watch": "cron_watch",
              # 4d-r3: the eight the s4d-r2 result listed as remaining
              "cascade.test": "cascade_test", "cascade.fix": "cascade_fix",
              "cascade.fallback": "cascade_fallback", "cron.triage": "cron_triage",
              "loop.review": "loop_review", "loop.test": "loop_test",
              "retry.failed_task": "retry_http", "watchdog.stale_review": "stale_review_http"}
NOT_YET_TRANSPORT_DRIVEN = sorted(set(DOCUMENTED) - set(TRANSPORTS))


def _app(db):
    from agent_crew.server import create_app
    return create_app(db_path=str(db), pane_map={}, port=0, watchdog_disabled=True,
                      anomaly_disabled=True)


def test_route_table_equals_the_documented_list():
    routes = {(m, r.path) for r in _app(__import__("tempfile").mktemp(suffix=".db")).routes
              if hasattr(r, "methods") for m in r.methods & {"POST", "PUT", "PATCH", "DELETE"}}
    assert routes == DOCUMENTED_MUTATING_ROUTES, (
        f"new: {sorted(routes - DOCUMENTED_MUTATING_ROUTES)}; "
        f"gone: {sorted(DOCUMENTED_MUTATING_ROUTES - routes)}")


def test_mcp_tool_registry_equals_the_documented_list(tmp_path):
    from agent_crew.mcp_server import build_mcp_server
    mcp = build_mcp_server(str(tmp_path / "m.db"))
    tools = set(mcp._tool_manager._tools)
    assert tools == DOCUMENTED_MCP_TOOLS, sorted(tools ^ DOCUMENTED_MCP_TOOLS)


def test_every_registered_adapter_is_transport_driven_or_listed_as_remaining():
    assert set(TRANSPORTS) <= set(adapters.BY_ID)
    # the remaining ids are reported in the step result; this pins the list so it
    # can only shrink knowingly.
    # 4d-r3: every documented ingress is now driven through its real entry point.
    assert NOT_YET_TRANSPORT_DRIVEN == []
    assert set(TRANSPORTS) == DOCUMENTED == set(adapters.BY_ID)


def _via_http(tmp_path, req, name):
    # raise_server_exceptions=False: observe the wire answer, as a real client does
    with TestClient(_app(tmp_path / name), raise_server_exceptions=False) as c:
        return c.post("/tasks", json=__import__("dataclasses").asdict(req))


def _drive(kind, tmp_path, live):
    """Run the real entry point for ``kind``; the spy records what reached admission."""
    db = tmp_path / f"{kind}.db"
    if kind == "http":
        _via_http(tmp_path, task("h1", context={"authority_decision_ids": ["T0-1234"]}),
                  f"{kind}.db")
    elif kind == "cli":
        from agent_crew.cli import crew
        CliRunner().invoke(crew, ["enqueue", "implement", "add a --json flag", "--db", str(db),
                                  "--task-id", "c1", "--project", "agent_crew"])
    elif kind == "loop":
        from agent_crew.loop import enqueue_implement
        from agent_crew.queue import AdmissionRefused, TaskQueue
        try:
            enqueue_implement(TaskQueue(str(db)), "add a --json flag", "main",
                              {"authority_decision_ids": ["T0-1234"]})
        except AdmissionRefused:
            pass
    elif kind == "discussion":
        from agent_crew.discussion import enqueue_panel_tasks
        from agent_crew.queue import AdmissionRefused, TaskQueue
        try:
            enqueue_panel_tasks(TaskQueue(str(db)), ["claude"], "A vs B", {})
        except AdmissionRefused:
            pass
    elif kind == "cascade_review":
        from agent_crew.pipeline import auto_enqueue_review
        from agent_crew.queue import TaskQueue
        target = live.s
        live.s = AuthorityState("active")
        q = TaskQueue(str(db))
        q.enqueue(task("impl-p", branch="feat/x"), ingress="http.tasks")
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE tasks SET status='completed' WHERE task_id='impl-p'")
        conn.commit()
        conn.close()
        live.s = target
        auto_enqueue_review(q, "impl-p", pr_number=None, pr_state_fn=lambda *a, **k: "OPEN")
    elif kind == "cron_watch":
        from agent_crew.queue import TaskQueue
        from agent_crew.watch import ClaimLedger, run_cycle

        class _Gh:
            issues = [{"number": 7, "title": "add a --json flag", "body": "",
                       "labels": [{"name": "agent-ready"}], "state": "OPEN"}]

            def list_issues(self, repo):
                return [dict(i) for i in self.issues]

            def add_label(self, *a):
                return True

            def remove_label(self, *a):
                return True

            def issue_has_open_pr(self, *a):
                return False
        run_cycle(queue=TaskQueue(str(db)), ledger=ClaimLedger(str(db)),
                  repo="example/agent_crew", gh=_Gh(), owner="t", project="agent_crew")


def _drive_r3(kind, db, live, monkeypatch):
    """4d-r3 transports. Parents are seeded under ACTIVE by the helpers; the
    transport itself runs under the target authority state."""
    from agent_crew.queue import AdmissionRefused, TaskQueue
    if kind == "cascade_test":
        from agent_crew.pipeline import auto_enqueue_test
        seed_finished_parent(db, live, task("rev-p", task_type="review", branch="feat/x"),
                             verdict="approve")
        auto_enqueue_test(TaskQueue(db), "rev-p", pr_state_fn=lambda *a, **k: "OPEN")
    elif kind == "cascade_fix":
        from agent_crew.pipeline import auto_enqueue_fix
        seed_finished_parent(db, live, task("rev-p", task_type="review", branch="feat/x"),
                             verdict="request_changes", findings=["P1: flag is ignored"])
        auto_enqueue_fix(TaskQueue(db), "rev-p", pr_state_fn=lambda *a, **k: "OPEN",
                         comment_fn=lambda *a, **k: None, suppress_side_effects=True)
    elif kind == "cascade_fallback":
        from agent_crew.pipeline import auto_fallback_failed_task
        from agent_crew.protocol import TaskResult
        seed_finished_parent(db, live, task("impl-f", context={"agent_override": "claude"}),
                             status="failed", summary="rate limit exceeded (429)")
        auto_fallback_failed_task(
            TaskQueue(db), "impl-f",
            TaskResult(task_id="impl-f", status="failed", summary="rate limit exceeded (429)"),
            "implement", state_path=str(db) + ".state.json", suppress_side_effects=True)
    elif kind == "cron_triage":
        from agent_crew.triage import enqueue_task
        try:
            enqueue_task(TaskQueue(db), {"parsed": {"issue": 7, "description": "add a --json flag"},
                                         "branch": "main"})
        except AdmissionRefused:
            pass
    elif kind in ("loop_review", "loop_test"):
        from agent_crew import loop
        fn = loop.enqueue_review if kind == "loop_review" else loop.enqueue_test
        try:
            fn(TaskQueue(db), "add a --json flag", "feat/x", "impl-1")
        except AdmissionRefused:
            pass
    elif kind == "retry_http":
        nonce = dispatch_and_start(db, live, task("impl-r", context={"authority_decision_ids":
                                                                     ["T0-1234"]}),
                                   role="implementer")
        with TestClient(_app(db), raise_server_exceptions=False) as c:
            c.post("/tasks/impl-r/result", json={
                "task_id": "impl-r", "status": "failed", "summary": "tests failed",
                "executor_binding": {"nonce": nonce, "presenter": "claude"}})
    elif kind == "stale_review_http":
        from agent_crew import server as server_mod
        from agent_crew.pipeline import ReviewPublication
        monkeypatch.setattr(server_mod, "review_publication_decision",
                            lambda *a, **k: ReviewPublication(
                                publish=False, status="stale_head", reason="head moved",
                                requeue_head="a" * 40))
        nonce = dispatch_and_start(db, live, task("rev-s", task_type="review", branch="feat/x",
                                                  context={"pr_number": 42}),
                                   role="reviewer", agent="codex")
        with TestClient(_app(db), raise_server_exceptions=False) as c:
            c.post("/tasks/rev-s/result", json={
                "task_id": "rev-s", "status": "completed", "verdict": "approve",
                "summary": "lgtm", "pr_number": 42,
                "executor_binding": {"nonce": nonce, "presenter": "codex"}})
    else:
        raise AssertionError(f"no driver for {kind}")


def _view(admitted, r):
    return (admitted, r["decision"], json.dumps(r["reason"], sort_keys=True), r["intent_hash"])


TRANSPORT_STATES = (AuthorityState("active"),
                    AuthorityState("quarantined", runtime=RuntimeState.QUARANTINED),
                    AuthorityState("budget-exhausted", budget=BudgetClass.EXHAUSTED))


S4F_EMPTY_PROJECT = ("s4f item 'empty-project admission': {kind} builds its TaskRequest with no "
                     "project (loop.enqueue_* / discussion.enqueue_panel_tasks); the engine RAISES "
                     "EngineError ($.project non-empty) instead of a P2 BLOCK receipt, so the "
                     "adapter crashes and nothing is persisted")


#: Transports whose adapter builds its TaskRequest with no ``project`` (checked by
#: ``test_every_transport_reaches_admission_as_its_own_ingress`` below: they reach
#: admission and the engine raises). 4d-r3 adds six to the s4d-r2 two.
EMPTY_PROJECT_KINDS = {"loop", "discussion", "cascade_test", "cascade_fallback", "cron_triage",
                       "loop_review", "loop_test", "retry_http", "stale_review_http"}

S4F_EMPTY_PROJECT = S4F_EMPTY_PROJECT.replace(
    "(loop.enqueue_* / discussion.enqueue_panel_tasks)",
    "(loop.enqueue_* / discussion.enqueue_panel_tasks / pipeline.auto_enqueue_test / "
    "pipeline.auto_fallback_failed_task / triage.enqueue_task / server _auto_retry_failed_task "
    "/ server _requeue_review_at_head)")


def _run(kind, tmp_path, live, monkeypatch):
    if kind in {"http", "cli", "loop", "discussion", "cascade_review", "cron_watch"}:
        _drive(kind, tmp_path, live)
    else:
        _drive_r3(kind, str(tmp_path / f"{kind}.db"), live, monkeypatch)


@pytest.mark.parametrize("kind", sorted(set(TRANSPORTS.values())))
def test_every_transport_reaches_admission_as_its_own_ingress(tmp_path, monkeypatch, kind):
    """Not xfailed for any transport: the driver is real, whatever admission then does.

    Separates "the entry point reaches the one admission entry under its own
    ingress id" (asserted for all fourteen) from "and the decision equals HTTP"
    (strict-xfailed where the s4f item is open). For the empty-project kinds it
    also pins *why* they fail, so the strict xfail cannot be failing for a
    different reason (a broken driver) without this test going red."""
    live = LiveState(AuthorityState("active"))
    inject_cea(monkeypatch, live)
    spy = EnqueueSpy(monkeypatch)
    try:
        _run(kind, tmp_path, live, monkeypatch)
    except Exception:
        pass  # the loop/discussion helpers let the engine's raise escape
    ingress_id = next(k for k, v in TRANSPORTS.items() if v == kind)
    mine = [c for c in spy.calls if c[0] == ingress_id]
    assert mine, f"{kind}: never reached admission as {ingress_id}; " \
                 f"calls={[c[0] for c in spy.calls]}"
    empty = [c for c in mine if not c[1].project]
    if kind in EMPTY_PROJECT_KINDS:
        assert empty and all(c[2] is None for c in empty), (
            f"{kind}: expected the empty-project raise; got {[(c[1].project, c[2]) for c in mine]}")
    else:
        assert not empty, f"{kind}: builds a project-less request but is not listed as such"


@pytest.mark.parametrize("state", TRANSPORT_STATES, ids=lambda s: s.label)
@pytest.mark.parametrize("kind", sorted(set(TRANSPORTS.values())))
def test_i1_transport_persisted_decision_equals_http(tmp_path, monkeypatch, kind, state,
                                                     request):
    if kind in EMPTY_PROJECT_KINDS:
        request.applymarker(pytest.mark.xfail(strict=True, raises=Exception,
                                              reason=S4F_EMPTY_PROJECT.format(kind=kind)))
    live = LiveState(state)
    inject_cea(monkeypatch, live)
    spy = EnqueueSpy(monkeypatch)
    _run(kind, tmp_path, live, monkeypatch)
    ingress_id = next(k for k, v in TRANSPORTS.items() if v == kind)
    mine = [c for c in spy.calls if c[0] == ingress_id]
    assert mine, f"{kind}: the real entry point never reached admission as {ingress_id}; " \
                 f"calls={[c[0] for c in spy.calls]}"
    for _, req, admitted, rid, db in mine:
        got = persisted_receipt(db, rid)
        assert got is not None, f"{kind}: no persisted receipt (P2 audit row missing)"
        assert not validate_receipt(got), validate_receipt(got)
        assert got["caller_identity_status"] == "UNVERIFIED"
        n = len(spy.calls)
        _via_http(tmp_path, req, f"base-{kind}-{req.task_id}.db")
        _, _, b_adm, b_rid, b_db = spy.calls[n]
        base = persisted_receipt(b_db, b_rid)
        assert _view(admitted, got) == _view(b_adm, base), (
            f"I1 transport violated: {kind} vs http.tasks under {state.label}")


@pytest.mark.xfail(strict=True, reason=(
    "s4f item 'empty-project admission': `crew enqueue --db` without --project and "
    "`watch.run_cycle(project='')` reach the engine with project='' and it RAISES a frozen-"
    "contract violation ($.project non-empty) instead of writing a P2 BLOCK audit receipt; "
    "the adapter surfaces an exception (cli exit 1 / watch 'enqueue failed') with no receipt"))
@pytest.mark.parametrize("kind", ["cli", "cron_watch"])
def test_empty_project_is_a_refusal_with_an_audit_receipt_not_an_exception(
        tmp_path, monkeypatch, kind):
    from agent_crew.queue import AdmissionRefused, TaskQueue
    live = LiveState(AuthorityState("active"))
    inject_cea(monkeypatch, live)
    q = TaskQueue(str(tmp_path / "e.db"))
    ingress = {"cli": "cli.enqueue", "cron_watch": "cron.watch"}[kind]
    with pytest.raises(AdmissionRefused) as exc:
        q.enqueue(task("e1", project=""), ingress=ingress)
    assert persisted_receipt(q._db_path, exc.value.receipt_id) is not None


@pytest.mark.xfail(strict=True, reason=(
    "s4f item 'empty-project admission': `crew enqueue --db` without --project and "
    "`watch.run_cycle(project='')` reach the engine with project='' and it RAISES a frozen-"
    "contract violation ($.project non-empty) instead of writing a P2 BLOCK audit receipt; "
    "the adapter surfaces an exception (cli exit 1 / watch 'enqueue failed') with no receipt"))
@pytest.mark.parametrize("kind", ["cli", "cron_watch"])
def test_empty_project_is_a_refusal_with_an_audit_receipt_not_an_exception(
        tmp_path, monkeypatch, kind):
    from agent_crew.queue import AdmissionRefused, TaskQueue
    live = LiveState(AuthorityState("active"))
    inject_cea(monkeypatch, live)
    q = TaskQueue(str(tmp_path / "e.db"))
    ingress = {"cli": "cli.enqueue", "cron_watch": "cron.watch"}[kind]
    with pytest.raises(AdmissionRefused) as exc:
        q.enqueue(task("e1", project=""), ingress=ingress)
    assert persisted_receipt(q._db_path, exc.value.receipt_id) is not None


@pytest.mark.xfail(strict=True, reason=(
    "s4f item 'http refusal mapping': POST /tasks has no `except AdmissionRefused` "
    "(server.py create_task: only TaskAlreadyExistsError -> 409) and no app exception handler "
    "for it, so a refused admission is an unhandled 500. Needs 4xx + receipt_id in the body"))
def test_http_refusal_is_a_4xx_carrying_the_receipt_id(tmp_path, monkeypatch):
    inject_cea(monkeypatch, LiveState(AuthorityState("q", runtime=RuntimeState.QUARANTINED)))
    resp = _via_http(tmp_path, task("h1"), "r.db")
    assert 400 <= resp.status_code < 500, resp.status_code
    assert "receipt_id" in resp.text
