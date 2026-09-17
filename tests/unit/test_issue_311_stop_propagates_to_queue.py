"""#311 — STOP has to reach the queue, not just the thing that feeds it.

Forensic from the parent incident: a feeder-level STOP prevented new dispatches
being handed to this runtime, and the runtime kept draining the tasks already in
its queue. STOP looked effective from above while work continued below it.

The gap is structural. Nothing in the execution path asked "am I allowed to
start anything right now" — the only guard lived in the supplier. So the gate
added here lives INSIDE `TaskQueue.dequeue`, the one place every transport
claims work: HTTP `/tasks/next`, the headless dispatcher loop, `_try_push_next`,
and MCP `get_next_task` all reach work through it. A guard there cannot be
walked around by changing how a caller asks — the same reasoning the MCP/HTTP
transport-parity fix rested on (#305 review).

⛔Generic on purpose. This runtime owns a pause STATE and a pause DECISION;
  deciding when a fleet should stop belongs to whatever drives it. Nothing here
  imports a fleet, and the global scope's path is env-driven with no default —
  a hardcoded shared location would be a deployment assumption a packaged
  runtime cannot make.
"""

import json
import os
import sqlite3

import pytest

from agent_crew.pause import (
    GLOBAL_PAUSE_FILE_ENV,
    GlobalPauseFile,
    PauseState,
    decide,
    resume_is_stale,
)
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


def _queued(q, n=3, task_type="implement"):
    for i in range(n):
        q.enqueue(TaskRequest(task_id=f"t{i}", task_type=task_type,
                              description=f"work {i}", branch="main", context={}))
    return q


# ── 1. the decision ───────────────────────────────────────────────────


def test_nothing_paused_allows_the_transition():
    assert decide([PauseState(scope="project"), PauseState(scope="global")]).allowed


def test_a_paused_project_blocks():
    decision = decide([PauseState(paused=True, scope="project", reason="incident")])
    assert decision.allowed is False
    assert decision.scope == "project"
    assert decision.reason == "incident"


def test_a_paused_global_blocks_even_when_the_project_is_running():
    """⛔A project must not run its way out of a runtime-wide STOP — that would
    make the global scope advisory, which is the shape of the reported bug."""
    decision = decide([PauseState(scope="project"),
                       PauseState(paused=True, scope="global", reason="fleet stop")])
    assert decision.allowed is False
    assert decision.scope == "global"


def test_the_global_scope_is_named_first_when_both_are_paused():
    """An operator clearing one STOP should be told about the wider one."""
    decision = decide([PauseState(paused=True, scope="project", reason="local"),
                       PauseState(paused=True, scope="global", reason="fleet")])
    assert decision.scope == "global"


def test_a_blocked_decision_always_says_why():
    """⛔A blocked transition that cannot name its scope, reason and generation
    is indistinguishable from a bug, and nobody can clear it."""
    decision = decide([PauseState(paused=True, scope="project", reason="incident",
                                  source="ops", incident_ref="ALFRED-39",
                                  generation=4)], transition="claim")
    payload = decision.to_dict()
    assert payload["blocked_by_scope"] == "project"
    assert payload["reason"] == "incident"
    assert payload["source"] == "ops"
    assert payload["incident_ref"] == "ALFRED-39"
    assert payload["generation"] == 4
    assert payload["transition"] == "claim"


# ── 2. the queue stops claiming ───────────────────────────────────────


def test_a_queue_with_work_drains_normally_when_not_paused(tmp_db):
    """⛔The control, and the one that matters most: a gate that blocks
    everything would 'fix' this by breaking the runtime."""
    q = _queued(TaskQueue(tmp_db))
    assert q.dequeue(role="implementer") is not None


def test_pause_stops_the_queue_draining(tmp_db):
    """★★The incident. Queued pre-STOP work must stay queued rather than
    continue to drain merely because it predates the STOP."""
    q = _queued(TaskQueue(tmp_db))
    q.activate_pause(reason="incident", source="ops", incident_ref="ALFRED-39")
    assert q.dequeue(role="implementer") is None
    assert q.dequeue() is None


def test_the_queued_work_is_still_there_afterwards(tmp_db):
    """⛔Blocked, never consumed or failed. 'Never silently mark work completed
    merely because STOP occurred' — the tasks must be waiting when we resume."""
    q = _queued(TaskQueue(tmp_db))
    q.activate_pause(reason="incident")
    q.dequeue(role="implementer")
    pending = [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "implement"]
    assert len(pending) == 3
    assert all(TaskQueue(tmp_db).get_task_status(t.task_id) == "pending" for t in pending)


def test_the_discuss_claim_path_is_gated_too(tmp_db):
    """⛔Both claim entry points, or the gate is one `dequeue_*` away from
    useless — the same one-transport half-measure #305's review caught."""
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="d1", task_type="discuss", description="topic",
                          branch="main", context={"agent": "claude"}))
    assert q.dequeue_discuss_for_agent("claude") is not None

    q2 = TaskQueue(tmp_db)
    q2.enqueue(TaskRequest(task_id="d2", task_type="discuss", description="topic",
                           branch="main", context={"agent": "claude"}))
    q2.activate_pause(reason="incident")
    assert q2.dequeue_discuss_for_agent("claude") is None


# ── 3. restart persistence ────────────────────────────────────────────


def test_a_pause_survives_a_restart(tmp_db):
    """★★Acceptance: restart while paused preserves the pause and still
    prevents the drain. A pause held only in memory would evaporate at exactly
    the moment an operator is restarting things to regain control."""
    _queued(TaskQueue(tmp_db))
    TaskQueue(tmp_db).activate_pause(reason="incident", incident_ref="ALFRED-39")

    reopened = TaskQueue(tmp_db)          # a fresh process would do exactly this
    assert reopened.pause_state().paused is True
    assert reopened.pause_state().incident_ref == "ALFRED-39"
    assert reopened.dequeue(role="implementer") is None


# ── 4. resume is generation-aware ─────────────────────────────────────


def test_resume_clears_the_pause_it_names(tmp_db):
    q = _queued(TaskQueue(tmp_db))
    state = q.activate_pause(reason="incident")
    released, _ = q.release_pause(state.generation)
    assert released is True
    assert q.dequeue(role="implementer") is not None


def test_a_stale_resume_is_refused(tmp_db):
    """★★Acceptance. A second incident landed after the operator read the
    status; the resume they typed is about a STOP that is no longer the reason
    work is halted, and honouring it would reopen the gate on an uncleared one."""
    q = _queued(TaskQueue(tmp_db))
    first = q.activate_pause(reason="first incident")
    q.activate_pause(reason="second incident")          # generation moves on

    released, state = q.release_pause(first.generation)
    assert released is False
    assert state.paused is True
    assert state.reason == "second incident"
    assert q.dequeue(role="implementer") is None, "a stale resume reopened the queue"


def test_a_resume_naming_no_generation_is_refused(tmp_db):
    """⛔It cannot prove it knows what it is clearing."""
    q = _queued(TaskQueue(tmp_db))
    q.activate_pause(reason="incident")
    assert q.release_pause(None)[0] is False
    assert q.dequeue(role="implementer") is None


def test_resume_does_not_duplicate_or_reset_the_lineage(tmp_db):
    """★★Acceptance: explicit current-generation resume continues the EXISTING
    lineage. A resume that re-created work would turn one incident into two
    copies of every task."""
    q = _queued(TaskQueue(tmp_db))
    before = [t.task_id for t in q.list_tasks()]
    state = q.activate_pause(reason="incident")
    q.release_pause(state.generation)

    after = [t.task_id for t in TaskQueue(tmp_db).list_tasks()]
    assert after == before, "resume changed the task set"
    resumed = q.dequeue(role="implementer")
    assert resumed is not None and resumed.task_id == "t0", "lineage order changed"


def test_the_generation_survives_a_resume(tmp_db):
    """⛔Monotonic. If resume reset it to zero, the next stale resume would be
    accepted and the whole guard would be one incident deep."""
    q = TaskQueue(tmp_db)
    first = q.activate_pause(reason="one")
    q.release_pause(first.generation)
    second = q.activate_pause(reason="two")
    assert second.generation > first.generation
    assert q.release_pause(first.generation)[0] is False


def test_a_resume_on_a_queue_that_is_not_paused_is_harmless(tmp_db):
    assert TaskQueue(tmp_db).release_pause(0)[0] is True


# ── 5. the global scope ───────────────────────────────────────────────


def test_no_global_file_configured_means_not_paused(monkeypatch):
    """⛔The standalone default. A packaged install with no fleet around it must
    run, so an unconfigured global scope cannot mean 'stopped'."""
    monkeypatch.delenv(GLOBAL_PAUSE_FILE_ENV, raising=False)
    assert GlobalPauseFile().configured is False
    assert GlobalPauseFile().read().paused is False


def test_a_global_pause_file_stops_this_project(tmp_db, tmp_path, monkeypatch):
    """★★An external system manager drives this through a stable interface —
    no import in either direction."""
    path = tmp_path / "global_pause.json"
    monkeypatch.setenv(GLOBAL_PAUSE_FILE_ENV, str(path))
    GlobalPauseFile().activate(reason="fleet stop", source="system-manager",
                               incident_ref="ALFRED-39")

    q = _queued(TaskQueue(tmp_db))
    assert q.dequeue(role="implementer") is None
    decision = q.pause_decision("claim")
    assert decision.scope == "global" and decision.incident_ref == "ALFRED-39"


def test_an_unreadable_global_pause_file_stops_rather_than_guesses(tmp_path, monkeypatch):
    """⛔Asymmetric, deliberately. 'A global STOP exists and I cannot read it'
    is exactly when guessing is unsafe; 'no global scope configured' is not."""
    path = tmp_path / "global_pause.json"
    path.write_text("{ not json at all")
    monkeypatch.setenv(GLOBAL_PAUSE_FILE_ENV, str(path))
    assert GlobalPauseFile().read().paused is True


def test_the_global_scope_is_generation_aware_too(tmp_path, monkeypatch):
    path = tmp_path / "global_pause.json"
    monkeypatch.setenv(GLOBAL_PAUSE_FILE_ENV, str(path))
    first = GlobalPauseFile().activate(reason="one")
    GlobalPauseFile().activate(reason="two")
    assert GlobalPauseFile().release(first.generation)[0] is False
    assert GlobalPauseFile().read().paused is True


def test_a_global_pause_survives_a_restart(tmp_path, monkeypatch):
    path = tmp_path / "global_pause.json"
    monkeypatch.setenv(GLOBAL_PAUSE_FILE_ENV, str(path))
    GlobalPauseFile().activate(reason="fleet stop")
    assert json.loads(path.read_text())["paused"] is True
    assert GlobalPauseFile().read().paused is True     # a fresh process


@pytest.mark.parametrize("generation", [None, "", "abc"])
def test_an_unparseable_resume_generation_is_stale(generation):
    assert resume_is_stale(PauseState(paused=True, generation=3), generation) is True


# ── 6. the standalone boundary ────────────────────────────────────────


def test_the_runtime_imports_no_fleet_package():
    """⛔The product boundary, as a test rather than a promise: external
    deployments may depend on Agent Crew, never the reverse.

    Checked by parsing imports, not grepping — the docstrings in this repo name
    those packages in prose, and a substring match would fail on the very files
    that document the rule."""
    import ast
    import pathlib

    import agent_crew

    root = pathlib.Path(agent_crew.__file__).resolve().parent
    forbidden = {"alfred", "quota_ops", "quota_core"}
    offenders = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                offenders += [f"{path.name}: {a.name}" for a in node.names
                              if a.name.split(".")[0] in forbidden]
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] in forbidden:
                    offenders.append(f"{path.name}: from {node.module}")
    assert offenders == [], offenders


def test_the_global_pause_path_has_no_baked_in_default(monkeypatch):
    """⛔A default shared path would be a deployment assumption. Config-driven
    or absent."""
    monkeypatch.delenv(GLOBAL_PAUSE_FILE_ENV, raising=False)
    assert GlobalPauseFile()._path == ""
    assert os.getenv(GLOBAL_PAUSE_FILE_ENV) is None


# ── 7. the operator / system-manager surface ──────────────────────────


def _client(tmp_db):
    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    return TestClient(create_app(db_path=tmp_db, pane_map={}, port=0,
                                 watchdog_disabled=True, anomaly_disabled=True,
                                 push_fn=lambda *a, **k: None))


def test_the_api_pauses_and_the_queue_stops(tmp_db):
    """★★End to end through the interface an external manager actually drives."""
    _queued(TaskQueue(tmp_db))
    with _client(tmp_db) as client:
        response = client.post("/pause", json={"reason": "incident",
                                               "source": "system-manager",
                                               "incident_ref": "ALFRED-39"})
        assert response.status_code == 200, response.text
        assert response.json()["state"]["generation"] >= 1
    assert TaskQueue(tmp_db).dequeue(role="implementer") is None


def test_the_status_endpoint_says_exactly_why_transitions_are_blocked(tmp_db):
    """Acceptance: status/telemetry shows why — scope, reason, source, incident
    and generation, not merely that something is blocked."""
    with _client(tmp_db) as client:
        client.post("/pause", json={"reason": "incident", "source": "system-manager",
                                    "incident_ref": "ALFRED-39"})
        status = client.get("/pause").json()
    decision = status["decision"]
    assert decision["allowed"] is False
    assert decision["blocked_by_scope"] == "project"
    assert decision["reason"] == "incident"
    assert decision["source"] == "system-manager"
    assert decision["incident_ref"] == "ALFRED-39"
    assert status["project"]["generation"] >= 1


def test_the_api_refuses_a_stale_resume_with_a_conflict(tmp_db):
    """⛔409, not 200-with-a-flag. A caller that only reads the status code must
    not read a refused resume as success."""
    _queued(TaskQueue(tmp_db))
    with _client(tmp_db) as client:
        first = client.post("/pause", json={"reason": "one"}).json()["state"]
        client.post("/pause", json={"reason": "two"})
        refused = client.post("/resume", json={"generation": first["generation"]})
        assert refused.status_code == 409, refused.text
    assert TaskQueue(tmp_db).dequeue(role="implementer") is None


def test_the_api_resumes_on_the_current_generation(tmp_db):
    _queued(TaskQueue(tmp_db))
    with _client(tmp_db) as client:
        state = client.post("/pause", json={"reason": "incident"}).json()["state"]
        ok = client.post("/resume", json={"generation": state["generation"]})
        assert ok.status_code == 200, ok.text
    assert TaskQueue(tmp_db).dequeue(role="implementer") is not None


def test_the_api_refuses_a_global_pause_with_no_file_configured(tmp_db, monkeypatch):
    """⛔Never invent a path. Asking for a scope this install has not configured
    is an error, not a silently project-scoped pause."""
    monkeypatch.delenv(GLOBAL_PAUSE_FILE_ENV, raising=False)
    with _client(tmp_db) as client:
        response = client.post("/pause", json={"scope": "global", "reason": "x"})
    assert response.status_code == 400


def test_an_unpaused_runtime_reports_itself_as_running(tmp_db):
    with _client(tmp_db) as client:
        status = client.get("/pause").json()
    assert status["decision"]["allowed"] is True
    assert status["project"]["paused"] is False


# ── 8. the CLI surface ────────────────────────────────────────────────


def _cli(args, base, monkeypatch):
    from click.testing import CliRunner

    from agent_crew.cli import crew

    return CliRunner().invoke(crew, args + ["--base", str(base)])


def _project_base(tmp_path, tmp_db):
    """Lay out `<base>/<project>/tasks.db` the way `crew setup` does."""
    import shutil

    TaskQueue(tmp_db)   # the fixture is a PATH; the file exists once a queue opens it
    base = tmp_path / "base"
    (base / "demo").mkdir(parents=True)
    shutil.copy(tmp_db, base / "demo" / "tasks.db")
    return base


def test_the_cli_pauses_resumes_and_reports(tmp_path, tmp_db, monkeypatch):
    """★★The standalone operator contract, end to end: pause, see why, resume."""
    _queued(TaskQueue(tmp_db))
    base = _project_base(tmp_path, tmp_db)
    db = str(base / "demo" / "tasks.db")

    paused = _cli(["pause", "demo", "--reason", "incident",
                   "--incident", "ALFRED-39"], base, monkeypatch)
    assert paused.exit_code == 0, paused.output
    assert "PAUSED" in paused.output
    assert TaskQueue(db).dequeue(role="implementer") is None

    status = _cli(["pause-status", "demo"], base, monkeypatch)
    assert "BLOCKED by project pause" in status.output
    assert "ALFRED-39" in status.output

    generation = TaskQueue(db).pause_state().generation
    resumed = _cli(["resume", "demo", "--generation", str(generation)], base, monkeypatch)
    assert resumed.exit_code == 0, resumed.output
    assert TaskQueue(db).dequeue(role="implementer") is not None


def test_the_cli_refuses_a_stale_resume(tmp_path, tmp_db, monkeypatch):
    _queued(TaskQueue(tmp_db))
    base = _project_base(tmp_path, tmp_db)
    db = str(base / "demo" / "tasks.db")

    _cli(["pause", "demo", "--reason", "one"], base, monkeypatch)
    stale = TaskQueue(db).pause_state().generation
    _cli(["pause", "demo", "--reason", "two"], base, monkeypatch)

    refused = _cli(["resume", "demo", "--generation", str(stale)], base, monkeypatch)
    assert refused.exit_code != 0, refused.output
    assert "stale" in refused.output.lower()
    assert TaskQueue(db).dequeue(role="implementer") is None


def test_pause_status_on_a_running_project(tmp_path, tmp_db, monkeypatch):
    base = _project_base(tmp_path, tmp_db)
    status = _cli(["pause-status", "demo"], base, monkeypatch)
    assert "RUNNING" in status.output


# ── 9. review of PR #312, reproduced against THIS branch ──────────────
#
# The review was written against a different implementation of #311 (PR #312,
# branch `safety/311-runtime-stop-pause`) — it cites `pause.is_paused()` and
# `tests/test_pause_stop.py`, neither of which exists here. Every finding was
# nonetheless checked against this branch, and all three reproduce. These are
# the regressions.


def test_a_stop_landing_during_a_claim_is_serialized_not_lost(tmp_db):
    """★★P0. The check used to run BEFORE the claim's transaction, so a STOP
    persisted between the check and the pending->in_progress commit still let a
    pre-STOP item start — after the safety boundary.

    The guarantee is serialization, and this pins it deterministically: a STOP
    is launched from INSIDE the claim's own pause read. It cannot interleave —
    both take `BEGIN IMMEDIATE` on the same database — so it must land either
    fully before the claim (refused) or fully after it. What it must never do is
    get lost, leaving the queue draining past the boundary.
    """
    import threading

    q = _queued(TaskQueue(tmp_db))
    original = TaskQueue._pause_state_on
    launched = {}

    def racing_read(self, conn, scope="project"):
        state = original(self, conn, scope)
        if scope == "project" and "thread" not in launched:
            def _stop():
                TaskQueue(tmp_db).activate_pause(reason="raced incident")

            launched["thread"] = threading.Thread(target=_stop)
            launched["thread"].start()
        return state

    TaskQueue._pause_state_on = racing_read
    try:
        claimed = q.dequeue(role="implementer")
    finally:
        TaskQueue._pause_state_on = original

    # The STOP was blocked behind the claim's write lock; let it finish now.
    launched["thread"].join(timeout=10.0)
    assert not launched["thread"].is_alive(), "the STOP never completed"

    final = TaskQueue(tmp_db).pause_state()
    assert final.paused is True, "the STOP was lost rather than serialized"
    assert TaskQueue(tmp_db).dequeue(role="implementer") is None, \
        "the queue kept draining after a STOP that landed during a claim"
    if claimed is not None:
        assert TaskQueue(tmp_db).get_task_status(claimed.task_id) == "in_progress"


def test_an_unreadable_project_pause_state_fails_closed(tmp_db):
    """★★P0. 'I could not determine the pause state' must never read as 'there
    is no pause'. Failing open here is the same error as reading an unreadable
    global STOP file as running."""
    q = _queued(TaskQueue(tmp_db))

    def _boom(self, conn, scope="project"):
        raise sqlite3.OperationalError("pause row unreadable")

    original = TaskQueue._pause_state_on
    TaskQueue._pause_state_on = _boom
    try:
        assert q.dequeue(role="implementer") is None, "claimed with unknown pause state"
    finally:
        TaskQueue._pause_state_on = original


def test_an_unreadable_global_pause_fails_closed_at_the_claim(tmp_db, tmp_path,
                                                              monkeypatch):
    """The global half of the same rule, at the claim rather than in isolation."""
    path = tmp_path / "global_pause.json"
    path.write_text("{ not json")
    monkeypatch.setenv(GLOBAL_PAUSE_FILE_ENV, str(path))
    q = _queued(TaskQueue(tmp_db))
    assert q.dequeue(role="implementer") is None


def test_a_blocked_claim_is_distinguishable_from_an_empty_queue(tmp_db):
    """★★P1. `dequeue` returning None told a caller nothing. A refusal is now a
    durable receipt carrying the scope, reason, incident and generation."""
    q = _queued(TaskQueue(tmp_db))
    q.activate_pause(reason="incident", source="system-manager",
                     incident_ref="ALFRED-39")
    q.dequeue(role="implementer")

    receipts = TaskQueue(tmp_db).list_blocked_transitions()
    assert receipts, "a blocked claim left no receipt"
    assert receipts[0]["transition"] == "claim"
    assert receipts[0]["scope"] == "project"
    assert receipts[0]["incident_ref"] == "ALFRED-39"
    assert receipts[0]["generation"] >= 1


def test_an_empty_queue_leaves_no_blocked_receipt(tmp_db):
    """⛔The control: an empty queue is not a refusal, and must not look like
    one — otherwise the receipt means nothing."""
    TaskQueue(tmp_db).dequeue(role="implementer")
    assert TaskQueue(tmp_db).list_blocked_transitions() == []


def test_concurrent_stop_and_resume_cannot_lose_the_newer_stop(tmp_db):
    """★★P1. Read-modify-write let a resume that had already read an older
    generation overwrite a STOP written in between — the comparison cannot see
    a write it never read. Compare and clear now happen in one transaction."""
    import threading

    q = TaskQueue(tmp_db)
    first = q.activate_pause(reason="first")

    barrier = threading.Barrier(2)
    results = {}

    def _resume():
        barrier.wait()
        results["released"] = TaskQueue(tmp_db).release_pause(first.generation)[0]

    def _stop():
        barrier.wait()
        TaskQueue(tmp_db).activate_pause(reason="second")

    threads = [threading.Thread(target=_resume), threading.Thread(target=_stop)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    final = TaskQueue(tmp_db).pause_state()
    if results.get("released") and final.generation == first.generation:
        pytest.fail("the resume cleared a pause while a newer STOP was landing")
    # Either the resume lost the race (still paused at the newer generation) or
    # it won and the newer STOP re-paused. Never: cleared AND a newer STOP lost.
    assert final.paused or final.generation >= first.generation


# ── 10. the cascades, per family (review of PR #312, P0) ──────────────


def _server(tmp_db):
    from fastapi.testclient import TestClient

    from agent_crew.server import create_app

    return TestClient(create_app(db_path=tmp_db, pane_map={}, port=0,
                                 watchdog_disabled=True, anomaly_disabled=True,
                                 push_fn=lambda *a, **k: None))


def _inflight(tmp_db, task_id="impl-1", task_type="implement", ctx=None):
    """A task already claimed and running when STOP arrives."""
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id=task_id, task_type=task_type,
                          description="in flight", branch="main",
                          context=ctx if ctx is not None else {}))
    claimed = q.dequeue(role={"implement": "implementer", "review": "reviewer"}
                        .get(task_type, "implementer"))
    assert claimed is not None and claimed.task_id == task_id
    return q


def test_an_inflight_result_under_stop_starts_no_review(tmp_db):
    """★★P0, the core of the finding. The claim gate alone let a result for a
    task already in flight go on creating the next stage."""
    _inflight(tmp_db)
    TaskQueue(tmp_db).activate_pause(reason="incident", incident_ref="ALFRED-39")

    with _server(tmp_db) as client:
        response = client.post("/tasks/impl-1/result", json={
            "task_id": "impl-1", "status": "completed", "summary": "done"})
        assert response.status_code == 200, response.text

    reviews = [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "review"]
    assert reviews == [], f"STOP did not prevent the next stage: {reviews}"


def test_the_inflight_result_itself_is_still_recorded(tmp_db):
    """⛔'Never silently mark work completed merely because STOP occurred' cuts
    both ways: the result that DID arrive must not be thrown away either. Only
    new work is refused."""
    _inflight(tmp_db)
    TaskQueue(tmp_db).activate_pause(reason="incident")
    with _server(tmp_db) as client:
        client.post("/tasks/impl-1/result", json={
            "task_id": "impl-1", "status": "completed", "summary": "keep me"})
    assert TaskQueue(tmp_db).get_result("impl-1") is not None


def test_a_failed_inflight_result_under_stop_starts_no_retry_or_fallback(tmp_db):
    """P0: retry and fallback are 'replacement tasks due to provider/recovery
    events' — requirement 2 lists them explicitly."""
    _inflight(tmp_db)
    TaskQueue(tmp_db).activate_pause(reason="incident")
    before = {t.task_id for t in TaskQueue(tmp_db).list_tasks()}

    with _server(tmp_db) as client:
        client.post("/tasks/impl-1/result", json={
            "task_id": "impl-1", "status": "failed",
            "summary": "rate limit", "error_info": {"reason": "rate_limit"}})

    after = {t.task_id for t in TaskQueue(tmp_db).list_tasks()}
    assert after == before, f"STOP did not prevent replacement work: {after - before}"


def test_an_approving_review_under_stop_starts_no_test_and_no_merge(
        tmp_db, github_writes, monkeypatch):
    """P0: the approve path drives a test enqueue and, with `no_tester`, a merge.

    ⛔Requests `github_writes` — the repo's boundary fixture fails any test that
      attempts a GitHub write, and an approving review publishes its verdict.
      Observing the calls is the sanctioned escape, and it doubles as the
      assertion: a merge under STOP would show up in the recording.
    """
    monkeypatch.setattr("agent_crew.github.pr_state", lambda *a, **k: "open")
    _inflight(tmp_db, task_id="review-1", task_type="review",
              ctx={"pr_number": 42, "repo": "owner/repo", "no_tester": True})
    TaskQueue(tmp_db).activate_pause(reason="incident")

    with _server(tmp_db) as client:
        response = client.post("/tasks/review-1/result", json={
            "task_id": "review-1", "status": "completed",
            "summary": "ok", "verdict": "approve", "pr_number": 42})
        assert response.status_code == 200, response.text

    tests = [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "test"]
    assert tests == [], f"STOP did not prevent a test stage: {tests}"
    merges = [c for c in github_writes if c.get("fn") == "merge_pr"]
    assert merges == [], f"STOP did not prevent a merge: {merges}"


def test_the_cascade_runs_normally_when_not_paused(tmp_db):
    """⛔The control for the whole section. Gating everything would 'fix' the
    incident by disabling the pipeline."""
    _inflight(tmp_db)
    with _server(tmp_db) as client:
        client.post("/tasks/impl-1/result", json={
            "task_id": "impl-1", "status": "completed", "summary": "done"})
    reviews = [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "review"]
    assert len(reviews) == 1, "the normal cascade stopped working"


def test_a_blocked_cascade_leaves_a_receipt_with_provenance(tmp_db):
    """P1: the blocked transition must be observable with task/context/provider
    identity, not merely absent."""
    _inflight(tmp_db, ctx={"context_id": "ctx-9", "agent": "claude",
                           "provider_session_id": "sess-77"})
    TaskQueue(tmp_db).activate_pause(reason="incident", incident_ref="ALFRED-39")
    with _server(tmp_db) as client:
        client.post("/tasks/impl-1/result", json={
            "task_id": "impl-1", "status": "completed", "summary": "done"})

    receipts = [r for r in TaskQueue(tmp_db).list_blocked_transitions()
                if r["transition"] == "review"]
    assert receipts, "a blocked cascade left no receipt"
    assert receipts[0]["task_id"] == "impl-1"
    assert receipts[0]["context_id"] == "ctx-9"
    assert receipts[0]["provider"] == "claude"
    assert receipts[0]["provider_session_id"] == "sess-77"
    assert receipts[0]["incident_ref"] == "ALFRED-39"


def test_the_cascade_fails_closed_when_the_pause_state_cannot_be_read(tmp_db):
    """⛔The cascade's half of fail-closed, which the claim tests do not cover.

    'I could not determine whether we are paused' must block new work, or a
    database hiccup during an incident quietly restarts the whole pipeline.
    """
    _inflight(tmp_db)

    def _boom(self, transition="claim", **kw):
        raise sqlite3.OperationalError("pause table unreadable")

    original = TaskQueue.pause_decision
    TaskQueue.pause_decision = _boom
    try:
        with _server(tmp_db) as client:
            response = client.post("/tasks/impl-1/result", json={
                "task_id": "impl-1", "status": "completed", "summary": "done"})
            assert response.status_code == 200, response.text
    finally:
        TaskQueue.pause_decision = original

    reviews = [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "review"]
    assert reviews == [], "the cascade ran with an unknown pause state"


def test_the_claim_fails_closed_when_the_global_read_raises(tmp_db, monkeypatch):
    """⛔Distinct from a malformed FILE, which `GlobalPauseFile.read` already
    turns into 'paused'. This is the read itself raising — a permission error, a
    vanished mount — where the except branch is the only thing standing between
    an unknown STOP and a draining queue."""
    from agent_crew import pause as pause_module

    _queued(TaskQueue(tmp_db))

    def _boom(self):
        raise OSError("global pause file unreadable")

    monkeypatch.setattr(pause_module.GlobalPauseFile, "read", _boom)
    assert TaskQueue(tmp_db).dequeue(role="implementer") is None


# ── 11. review round 2: the transports, recovery, and fail-open ───────
#
# Three P0s, all reproduced against this branch before fixing:
#   * MCP called the pipeline helpers directly, so every cascade guard added in
#     round 1 — which lived in the HTTP server's wrappers — was bypassed by
#     choosing a different transport;
#   * automatic recovery/requeue ran under STOP, mutating in-flight lineage past
#     the safe boundary;
#   * `pause_decision`, the shared decision every cascade gate uses, still
#     caught an exceptional global read and continued on project state alone.


def _mcp(tmp_db):
    from agent_crew.mcp_server import build_mcp_server

    return build_mcp_server(tmp_db)


def _mcp_call(mcp, tool, **kwargs):
    import asyncio

    fn = mcp._tool_manager._tools[tool].fn
    return asyncio.run(fn(**kwargs)) if asyncio.iscoroutinefunction(fn) else fn(**kwargs)


def _mcp_submit(tmp_db, task_id, **fields):
    mcp = _mcp(tmp_db)
    ack = _mcp_call(mcp, "submit_result", task_id=task_id, **fields)
    assert ack.get("acknowledged") is True, ack
    return ack


def test_an_mcp_result_under_stop_starts_no_review(tmp_db):
    """★★P0. The round-1 gate lived in the HTTP wrappers; MCP calls the pipeline
    helpers directly and walked straight past it."""
    _inflight(tmp_db)
    TaskQueue(tmp_db).activate_pause(reason="incident", incident_ref="ALFRED-39")
    _mcp_submit(tmp_db, "impl-1", status="completed", summary="done")
    reviews = [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "review"]
    assert reviews == [], f"MCP bypassed the cascade gate: {reviews}"


def test_an_mcp_result_under_stop_starts_no_retry_or_fallback(tmp_db):
    _inflight(tmp_db)
    TaskQueue(tmp_db).activate_pause(reason="incident")
    before = {t.task_id for t in TaskQueue(tmp_db).list_tasks()}
    _mcp_submit(tmp_db, "impl-1", status="failed", summary="rate limit")
    after = {t.task_id for t in TaskQueue(tmp_db).list_tasks()}
    assert after == before, f"MCP created replacement work under STOP: {after - before}"


def test_an_mcp_approving_review_under_stop_starts_no_test(tmp_db, github_writes,
                                                           monkeypatch):
    monkeypatch.setattr("agent_crew.github.pr_state", lambda *a, **k: "open")
    _inflight(tmp_db, task_id="review-1", task_type="review",
              ctx={"pr_number": 42, "repo": "owner/repo"})
    TaskQueue(tmp_db).activate_pause(reason="incident")
    _mcp_submit(tmp_db, "review-1", status="completed", summary="ok",
                verdict="approve", pr_number=42)
    tests = [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "test"]
    assert tests == [], f"MCP started a test stage under STOP: {tests}"


def test_an_mcp_rejecting_review_under_stop_starts_no_fix(tmp_db, github_writes,
                                                          monkeypatch):
    monkeypatch.setattr("agent_crew.github.pr_state", lambda *a, **k: "open")
    _inflight(tmp_db, task_id="review-1", task_type="review",
              ctx={"pr_number": 42, "repo": "owner/repo"})
    TaskQueue(tmp_db).activate_pause(reason="incident")
    _mcp_submit(tmp_db, "review-1", status="completed", summary="no",
                verdict="request_changes", findings=["code_quality: x"], pr_number=42)
    fixes = [t for t in TaskQueue(tmp_db).list_tasks() if t.task_id.startswith("fix-")]
    assert fixes == [], f"MCP started a fix round under STOP: {fixes}"


def test_the_mcp_cascade_runs_normally_when_not_paused(tmp_db):
    """⛔The control. Gating MCP into uselessness would 'fix' this by breaking
    the transport."""
    _inflight(tmp_db)
    _mcp_submit(tmp_db, "impl-1", status="completed", summary="done")
    reviews = [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "review"]
    assert len(reviews) == 1, "the MCP cascade stopped working"


def test_recovery_requeue_is_blocked_under_stop(tmp_db):
    """★★P0. Requirement 2 prohibits automatic recover/requeue while paused, and
    requirement 3 asks for the lineage to be RETAINED — so an in-flight task
    stays in_progress rather than being reset by a restart during an incident."""
    _inflight(tmp_db)
    TaskQueue(tmp_db).activate_pause(reason="incident", incident_ref="ALFRED-39")

    TaskQueue(tmp_db).requeue("impl-1")
    assert TaskQueue(tmp_db).get_task_status("impl-1") == "in_progress", \
        "STOP did not prevent the lineage being mutated past the safe boundary"

    receipts = [r for r in TaskQueue(tmp_db).list_blocked_transitions()
                if r["transition"] == "recovery"]
    assert receipts and receipts[0]["task_id"] == "impl-1"


def test_recovery_requeue_works_normally_when_not_paused(tmp_db):
    """⛔The control: recovery is how a dead worker's task gets picked up again."""
    _inflight(tmp_db)
    TaskQueue(tmp_db).requeue("impl-1")
    assert TaskQueue(tmp_db).get_task_status("impl-1") == "pending"


def test_a_restart_while_paused_does_not_requeue_in_progress_work(tmp_db):
    """★★The restart path the finding names: a dispatcher coming back up during
    an incident must not sweep every in-flight task back to pending."""
    _inflight(tmp_db)
    TaskQueue(tmp_db).activate_pause(reason="incident")

    with _server(tmp_db):          # startup runs the orphan sweep
        pass
    assert TaskQueue(tmp_db).get_task_status("impl-1") == "in_progress"


@pytest.mark.parametrize("transport", ["http", "mcp"])
def test_an_exceptional_global_read_blocks_the_cascade(tmp_db, transport, monkeypatch):
    """★★P0. `pause_decision` — the decision every cascade gate consults —
    caught an exceptional global read and carried on with project state alone,
    so an unavailable global STOP could reopen review/retry/test/fix/merge while
    the claim stayed blocked."""
    from agent_crew import pause as pause_module

    _inflight(tmp_db)

    def _boom(self):
        raise OSError("global pause file unreadable")

    monkeypatch.setattr(pause_module.GlobalPauseFile, "read", _boom)
    if transport == "http":
        with _server(tmp_db) as client:
            client.post("/tasks/impl-1/result", json={
                "task_id": "impl-1", "status": "completed", "summary": "done"})
    else:
        _mcp_submit(tmp_db, "impl-1", status="completed", summary="done")

    reviews = [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "review"]
    assert reviews == [], f"{transport} cascade ran with an unknown global STOP"


def test_the_pipeline_gate_fails_closed_when_the_decision_itself_raises(tmp_db):
    """⛔The pipeline's OWN except branch, which the HTTP test cannot reach —
    the server's gate catches the error first and blocks before the pipeline is
    consulted. MCP calls the pipeline helpers directly, so it is the only
    transport that exercises this path."""
    _inflight(tmp_db)

    def _boom(self, transition="claim", **kw):
        raise sqlite3.OperationalError("pause table unreadable")

    original = TaskQueue.pause_decision
    TaskQueue.pause_decision = _boom
    try:
        _mcp_submit(tmp_db, "impl-1", status="completed", summary="done")
    finally:
        TaskQueue.pause_decision = original

    reviews = [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "review"]
    assert reviews == [], "the pipeline cascade ran with an unknown pause state"
