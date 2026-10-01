"""T5: "dispatch base absent" is a FAIL, never a pass (ADR §11.1 row 10, CXC-6a).

An implement task that reports `completed` with no declared artifact contract
and no `worktree_base_sha`/`reviewed_sha` gives the artifact gate nothing to
check the completion against. The server used to log

    artifact gate not applied — dispatch base absent

and accept the result. That turned an *absent input* into a passing check, which
is precisely what P7 forbids: a gate that cannot compute an answer must not
invent a favourable one. The missing base is not an exemption from the rule, it
is the reason the rule cannot be satisfied.

⛔Held under the project's rollout mode, not unconditionally. Under `shadow`
  (the default, and what live projects run) the finding is logged and the result
  stands — measuring how much real traffic this catches is the entire purpose of
  the shadow phase, and flipping a live fleet to refusal on the same commit that
  introduces the rule is how a gate gets reverted instead of adopted.
"""

import types

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue

from tests.unit.test_sev0_cea_s2c_writer_callsites import WIRED, admitted

UNREACHABLE_PANE = "no-such-session:0.0"
MODE = "AGENT_CREW_CEA_MODE"


def _app(tmp_db):
    from agent_crew.server import create_app

    return TestClient(create_app(tmp_db, pane_map={"reviewer": UNREACHABLE_PANE},
                                 watchdog_disabled=True, anomaly_disabled=True,
                                 worktree_map={}, push_fn=lambda *a, **k: None))


@pytest.fixture
def hermetic_admission(monkeypatch):
    """Pinned providers everywhere, so the suite reads no production input.

    ⛔Without this the server's queue wires itself from ``install_from_env``,
      i.e. the live ``~/alfred/governance`` snapshot — unkeyed, therefore an
      unverified input, so admission BLOCKs (P7). That is the same leak s4g
      closed in the s4b harness. It also matters for *what these tests mean*:
      the enforce cases below need a receipt the gates actually accept, which a
      BLOCK receipt is not.
    """
    from agent_crew.cea import wiring as cea_wiring

    original = TaskQueue.__init__

    def patched(self, db_path, **kw):
        kw["cea_providers"] = dict(WIRED)      # modes still come from the env
        original(self, db_path, **kw)

    monkeypatch.setattr(TaskQueue, "__init__", patched)
    monkeypatch.setattr(cea_wiring, "install_from_env", lambda *a, **k: types.SimpleNamespace(
        providers=dict(WIRED), authority=None, mode="shadow", statuses=()))


def _run_to_result(client, task_id, body):
    """Claim → dispatch → start → result, over the real transport.

    ⛔Step 4i made the four post-admission gates read the *receipt's* project,
      so a project pinned to ``enforce`` is enforced at claim, dispatch, start
      and result — not only at the T5 artifact gate. A result posted for a row
      that was never claimed is now refused by P2 before T5 is reached, which
      would test the wrong rule. Driving the lifecycle is what isolates the
      artifact gate now that the lineage is enforced consistently.
    """
    handed = client.get("/tasks/next",
                        params={"role": "implementer", "agent": "claude"}).json()
    assert handed.get("task_id") == task_id, handed
    nonce = handed.get("dispatch_nonce")
    assert nonce, handed
    go = client.post(f"/tasks/{task_id}/start",
                     json={"nonce": nonce, "presenter": "claude"}).json()
    assert go["go"] is True, go
    body = dict(body, executor_binding={"nonce": nonce, "presenter": "claude"})
    return client.post(f"/tasks/{task_id}/result", json=body)


def _baseless_implement(tmp_db, task_id="impl-nobase", project="demo"):
    """The shape under test: an implement task with **no** dispatch base and no
    declared artifact contract — the case the old code waved through.

    ⛔Seeded before the rollout mode is raised. Admission under `enforce` refuses
      a receipt-less direct enqueue, which is a *different* gate (T1) and would
      hide the one being tested here. Rows admitted under shadow and finished
      under enforce are also the realistic rollout shape."""
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id, "implement", "change", branch="feat/work",
                              project=project, context=admitted()))
    return queue


def _row(queue, task_id):
    return next(t for t in queue.list_all_with_status() if t["task_id"] == task_id)


def test_under_enforce_a_baseless_completion_is_held(tmp_db, monkeypatch,
                                                    hermetic_admission):
    """⛔Enforce is selected with the **per-project** variable, and that is not
      cosmetic. It is what lets one project hold this gate while the rest of the
      fleet keeps measuring — the case §8 added it for.

      The task is driven claim → dispatch → start before the result is posted.
      Step 4i made the post-admission gates read the receipt's project too, so
      under ``enforce`` a result for a row that was never claimed is refused by
      P2 (`RECEIPT_STATE_INVALID`) before T5 is reached. Running the real
      lifecycle is what isolates the artifact gate; skipping it would assert a
      refusal that comes from somewhere else."""
    queue = _baseless_implement(tmp_db, project="hot")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE__HOT", "enforce")
    with _app(tmp_db) as client:
        response = _run_to_result(client, "impl-nobase", {
            "task_id": "impl-nobase", "status": "completed", "summary": "tests green"})

    assert response.status_code == 200, response.text
    assert response.json()["held"] == "no_artifact"
    row = _row(queue, "impl-nobase")
    assert row["status"] == "failed"
    assert row["error_info"]["reason"] == "no_artifact"
    assert "dispatch base absent" in row["error_info"]["detail"], \
        "the held row must say why it could not be verified, not merely that it was not"


def test_under_enforce_the_review_cascade_never_starts(tmp_db, monkeypatch,
                                                      hermetic_admission):
    """The consequence that matters. An unverifiable completion that is accepted
    spends a reviewer invocation on work nobody can point at.

    Driven through the full lifecycle for the same reason as above: a result
    refused at P2 would also produce no review task, and would say nothing about
    the artifact gate."""
    queue = _baseless_implement(tmp_db, project="hot")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE__HOT", "enforce")
    with _app(tmp_db) as client:
        response = _run_to_result(client, "impl-nobase", {
            "task_id": "impl-nobase", "status": "completed", "summary": "tests green"})
    assert response.json()["held"] == "no_artifact", response.text
    assert not [t for t in queue.list_tasks() if t.task_type == "review"]


def test_under_shadow_the_result_still_stands(tmp_db, monkeypatch):
    monkeypatch.setenv(MODE, "shadow")
    queue = _baseless_implement(tmp_db)
    with _app(tmp_db) as client:
        response = client.post("/tasks/impl-nobase/result", json={
            "task_id": "impl-nobase", "status": "completed", "summary": "tests green"})

    assert response.status_code == 200
    assert response.json().get("held") != "no_artifact"
    assert _row(queue, "impl-nobase")["status"] == "completed"


def test_the_rollout_mode_is_read_per_project(tmp_db, monkeypatch, hermetic_admission):
    """⛔The whole reason the per-project variable exists: one project can hold
    this gate while another is still measuring, in one server process."""
    monkeypatch.setenv(MODE, "shadow")
    queue = _baseless_implement(tmp_db, "impl-hot", project="hot")
    _baseless_implement(tmp_db, "impl-cold", project="cold")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE__HOT", "enforce")
    with _app(tmp_db) as client:
        hot = _run_to_result(client, "impl-hot", {
            "task_id": "impl-hot", "status": "completed", "summary": "green"})
        cold = _run_to_result(client, "impl-cold", {
            "task_id": "impl-cold", "status": "completed", "summary": "green"})

    assert hot.json()["held"] == "no_artifact", hot.text
    assert cold.json().get("held") != "no_artifact"
    assert _row(queue, "impl-hot")["status"] == "failed"
    assert _row(queue, "impl-cold")["status"] == "completed"


def test_a_task_that_does_have_a_base_is_unaffected(tmp_db, monkeypatch):
    """This change adds a FAIL for the unverifiable case; it does not touch the
    #353 rule for tasks that can be checked — which is mode-independent, so this
    runs under the default `shadow` exactly as a live project does."""
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("impl-based", "implement", "change", branch="feat/work",
                              project="demo", context={"worktree_base_sha": "a" * 40}))
    monkeypatch.setenv(MODE, "shadow")
    monkeypatch.setattr("agent_crew.server.verify_implement_artifact",
                        lambda *_a, **_k: (True, "test evidence"))
    with _app(tmp_db) as client:
        response = client.post("/tasks/impl-based/result", json={
            "task_id": "impl-based", "status": "completed", "summary": "green",
            "commit": "b" * 40})
    assert response.json().get("held") != "no_artifact"
    assert _row(queue, "impl-based")["status"] == "completed"


def test_the_server_no_longer_says_not_applied(caplog, tmp_db, monkeypatch):
    """The exact wording is the bug: "not applied" told a reader the rule did
    not cover this task, when it covered it and could not be satisfied."""
    import inspect

    import agent_crew.server as server_module
    src = inspect.getsource(server_module)
    assert "artifact gate not applied" not in src
