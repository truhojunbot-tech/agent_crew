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
