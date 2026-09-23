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

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue

UNREACHABLE_PANE = "no-such-session:0.0"
MODE = "AGENT_CREW_CEA_MODE"


def _app(tmp_db):
    from agent_crew.server import create_app

    return TestClient(create_app(tmp_db, pane_map={"reviewer": UNREACHABLE_PANE},
                                 watchdog_disabled=True, anomaly_disabled=True,
                                 worktree_map={}, push_fn=lambda *a, **k: None))


def _baseless_implement(tmp_db, task_id="impl-nobase", project="demo"):
    """The shape under test: an implement task with **no** dispatch base and no
    declared artifact contract — the case the old code waved through.

    ⛔Seeded before the rollout mode is raised. Admission under `enforce` refuses
      a receipt-less direct enqueue, which is a *different* gate (T1) and would
      hide the one being tested here. Rows admitted under shadow and finished
      under enforce are also the realistic rollout shape."""
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id, "implement", "change", branch="feat/work",
                              project=project, context={}))
    return queue


def _row(queue, task_id):
    return next(t for t in queue.list_all_with_status() if t["task_id"] == task_id)


def test_under_enforce_a_baseless_completion_is_held(tmp_db, monkeypatch):
    """⛔Enforce is selected with the **per-project** variable, and that is not
      cosmetic. Setting the process-wide `AGENT_CREW_CEA_MODE=enforce` instead
      makes `/result` answer 409 before the artifact gate is reached — the s4b
      RESULT rule (a nonce proves a start happened) fires on a task this test
      never dispatched. Measured while writing this test, and it is a true
      statement about the half-wired state recorded in the fold plan: the
      artifact check here reads the task's project, while the four T3 call sites
      still read the process-wide mode. Asserting through the per-project
      variable isolates the gate under test instead of asserting a 409 that
      comes from somewhere else."""
    queue = _baseless_implement(tmp_db, project="hot")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE__HOT", "enforce")
    with _app(tmp_db) as client:
        response = client.post("/tasks/impl-nobase/result", json={
            "task_id": "impl-nobase", "status": "completed", "summary": "tests green"})

    assert response.status_code == 200
    assert response.json()["held"] == "no_artifact"
    row = _row(queue, "impl-nobase")
    assert row["status"] == "failed"
    assert row["error_info"]["reason"] == "no_artifact"
    assert "dispatch base absent" in row["error_info"]["detail"], \
        "the held row must say why it could not be verified, not merely that it was not"


def test_under_enforce_the_review_cascade_never_starts(tmp_db, monkeypatch):
    """The consequence that matters. An unverifiable completion that is accepted
    spends a reviewer invocation on work nobody can point at."""
    queue = _baseless_implement(tmp_db, project="hot")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE__HOT", "enforce")
    with _app(tmp_db) as client:
        client.post("/tasks/impl-nobase/result", json={
            "task_id": "impl-nobase", "status": "completed", "summary": "tests green"})
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


def test_the_rollout_mode_is_read_per_project(tmp_db, monkeypatch):
    """⛔The whole reason the per-project variable exists: one project can hold
    this gate while another is still measuring, in one server process."""
    monkeypatch.setenv(MODE, "shadow")
    queue = _baseless_implement(tmp_db, "impl-hot", project="hot")
    _baseless_implement(tmp_db, "impl-cold", project="cold")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE__HOT", "enforce")
    with _app(tmp_db) as client:
        hot = client.post("/tasks/impl-hot/result", json={
            "task_id": "impl-hot", "status": "completed", "summary": "green"})
        cold = client.post("/tasks/impl-cold/result", json={
            "task_id": "impl-cold", "status": "completed", "summary": "green"})

    assert hot.json()["held"] == "no_artifact"
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
