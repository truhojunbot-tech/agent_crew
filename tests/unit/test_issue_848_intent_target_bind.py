"""#848: only an explicit directive target permits a cross-issue intent bind."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


OP_ID = "alfred44:6403744303"


@pytest.fixture
def crew(tmp_path):
    db = str(tmp_path / "tasks.db")
    app = create_app(db, project="alfred", pane_map={},
                     watchdog_disabled=True, anomaly_disabled=True,
                     identity_required=False)
    with TestClient(app) as client:
        yield client, TaskQueue(db)


def _intent(**changes):
    return {"external_operation_id": OP_ID, "project": "alfred",
            "repo": "owner/alfred", "issue": 44,
            "coordinator_identity": "alfred-owner",
            "coordinator_session": "session-1", "priority": 1,
            "risk_tier": 2, "execution_slo_s": 60, **changes}


def _task(queue, task_id, *, issue=405, repo="owner/alfred", tier=2,
          project="alfred", task_type="implement", status="pending"):
    context = {"risk_tier": tier, "repo": repo}
    if issue is not None:
        context["issue"] = issue
    queue.enqueue(TaskRequest(
        task_id=task_id, task_type=task_type, description="Canonical work",
        branch="agent/405", project=project, context=context))
    if status != "pending":
        with sqlite3.connect(queue.db_path) as conn:
            conn.execute("UPDATE tasks SET status=? WHERE task_id=?", (status, task_id))


def _bind(client, task_id):
    return client.post(f"/work-intents/{OP_ID}/bind", json={"task_id": task_id})


def test_same_issue_bind_stays_allowed(crew):
    client, queue = crew
    assert client.post("/work-intents/admit", json=_intent()).status_code == 201
    _task(queue, "impl-44", issue=44)
    response = _bind(client, "impl-44")
    assert response.status_code == 200
    assert response.json()["state"] == "BOUND"
    assert queue.get_task("impl-44").context["issue"] == 44


@pytest.mark.parametrize("task_tier", [2, 3])
def test_bind_accepts_task_at_or_below_intent_tier(crew, task_tier):
    client, queue = crew
    assert client.post("/work-intents/admit", json=_intent(risk_tier=3)).status_code == 201
    _task(queue, "impl-44", issue=44, tier=task_tier)

    response = _bind(client, "impl-44")

    assert response.status_code == 200
    assert response.json()["state"] == "BOUND"
    assert queue.get_task("impl-44").context["risk_tier"] == task_tier


def test_bind_refuses_task_above_intent_tier(crew):
    client, queue = crew
    client.post("/work-intents/admit", json=_intent(risk_tier=1))
    _task(queue, "impl-44", issue=44, tier=2)

    response = _bind(client, "impl-44")

    assert response.status_code == 409
    assert "risk_tier" in response.json()["detail"]
    assert queue.get_work_intent(OP_ID)["task_ids"] == []


@pytest.mark.parametrize("invalid_tier", [True, 4, "2"])
def test_bind_refuses_invalid_declared_task_tier(crew, invalid_tier):
    client, queue = crew
    client.post("/work-intents/admit", json=_intent(risk_tier=3))
    _task(queue, "impl-44", issue=44)
    with sqlite3.connect(queue.db_path) as conn:
        context = queue.get_task("impl-44").context
        context["risk_tier"] = invalid_tier
        conn.execute("UPDATE tasks SET context=? WHERE task_id='impl-44'",
                     (json.dumps(context),))

    response = _bind(client, "impl-44")

    assert response.status_code == 409
    assert "risk_tier" in response.json()["detail"]
    assert queue.get_work_intent(OP_ID)["task_ids"] == []


def test_bind_without_declared_tier_inherits_intent_tier(crew):
    client, queue = crew
    client.post("/work-intents/admit", json=_intent(risk_tier=3))
    _task(queue, "impl-44", issue=44)
    with sqlite3.connect(queue.db_path) as conn:
        context = queue.get_task("impl-44").context
        del context["risk_tier"]
        conn.execute("UPDATE tasks SET context=? WHERE task_id='impl-44'",
                     (json.dumps(context),))

    assert _bind(client, "impl-44").status_code == 200
    assert queue.get_task("impl-44").context["risk_tier"] == 3


def test_admit_with_task_cannot_declare_target(crew):
    client, queue = crew
    task = {"task_id": "impl-44", "task_type": "implement",
            "description": "Thread work", "branch": "agent/44", "project": "alfred",
            "context": {"issue": 44, "risk_tier": 2, "repo": "owner/alfred"}}

    response = client.post("/work-intents/admit", json=_intent(
        task=task, target_repo="owner/alfred", target_issue=405))

    assert response.status_code == 422
    assert queue.get_work_intent(OP_ID) is None
    assert queue.get_task("impl-44") is None


def test_task_bound_intent_cannot_gain_target_on_replay(crew):
    client, queue = crew
    task = {"task_id": "impl-44", "task_type": "implement",
            "description": "Thread work", "branch": "agent/44", "project": "alfred",
            "context": {"issue": 44, "risk_tier": 2, "repo": "owner/alfred"}}
    admitted = client.post("/work-intents/admit", json=_intent(task=task))
    assert admitted.status_code == 201
    assert admitted.json()["task_ids"] == ["impl-44"]

    replay = client.post("/work-intents/admit", json=_intent(
        target_repo="owner/alfred", target_issue=405))
    replay_with_task = client.post("/work-intents/admit", json=_intent(
        task=task, target_repo="owner/alfred", target_issue=405))

    assert replay.status_code == 201
    assert replay.json()["target_issue"] is None
    assert replay_with_task.status_code == 201
    assert replay_with_task.json()["target_issue"] is None
    _task(queue, "impl-405")
    assert _bind(client, "impl-405").status_code == 409
    assert queue.get_work_intent(OP_ID)["task_ids"] == ["impl-44"]


def test_missing_task_issue_inherits_thread_issue(crew):
    client, queue = crew
    client.post("/work-intents/admit", json=_intent())
    _task(queue, "impl-no-issue", issue=None)
    response = _bind(client, "impl-no-issue")
    assert response.status_code == 200
    assert queue.get_task("impl-no-issue").context["issue"] == 44


def test_completed_same_issue_test_reconciles_on_bind(crew):
    client, queue = crew
    client.post("/work-intents/admit", json=_intent())
    _task(queue, "test-44", issue=44, task_type="test", status="completed")
    response = _bind(client, "test-44")
    assert response.status_code == 200
    assert response.json()["state"] == "SUCCESS"


def test_explicit_target_binds_pending_canonical_task_without_rewriting_it(crew):
    client, queue = crew
    response = client.post("/work-intents/admit", json=_intent(
        target_repo="owner/alfred", target_issue=405))
    assert response.status_code == 201
    assert (response.json()["target_repo"], response.json()["target_issue"]) == (
        "owner/alfred", 405)
    _task(queue, "impl-405")

    bound = _bind(client, "impl-405")

    assert bound.status_code == 200
    assert (bound.json()["state"], bound.json()["task_ids"]) == ("BOUND", ["impl-405"])
    assert (bound.json()["issue"], bound.json()["risk_tier"]) == (44, 2)
    assert queue.get_task("impl-405").context["issue"] == 405
    assert queue.get_task("impl-405").context["repo"] == "owner/alfred"


def test_explicit_target_bind_accepts_lower_task_tier(crew):
    client, queue = crew
    client.post("/work-intents/admit", json=_intent(
        risk_tier=3, target_repo="owner/alfred", target_issue=405))
    _task(queue, "impl-405", tier=1)

    response = _bind(client, "impl-405")

    assert response.status_code == 200
    assert response.json()["state"] == "BOUND"
    assert queue.get_task("impl-405").context["risk_tier"] == 1
    assert queue.get_task("impl-405").context["issue"] == 405


def test_admit_with_embedded_task_keeps_strict_tier_check(crew):
    client, queue = crew
    task = {"task_id": "impl-44", "task_type": "implement",
            "description": "Thread work", "branch": "agent/44", "project": "alfred",
            "context": {"issue": 44, "risk_tier": 2, "repo": "owner/alfred"}}

    response = client.post("/work-intents/admit", json=_intent(
        risk_tier=3, task=task))

    assert response.status_code == 422
    assert queue.get_work_intent(OP_ID) is None
    assert queue.get_task("impl-44") is None


def test_explicit_target_can_name_a_different_repo(crew):
    client, queue = crew
    client.post("/work-intents/admit", json=_intent(
        target_repo="owner/canonical", target_issue=405))
    _task(queue, "impl-canonical", repo="owner/canonical")

    response = _bind(client, "impl-canonical")

    assert response.status_code == 200
    assert queue.get_task("impl-canonical").context["repo"] == "owner/canonical"
    assert response.json()["repo"] == "owner/alfred"


def test_blocked_replay_sets_target_once_then_completed_test_reconciles(crew):
    client, queue = crew
    client.post("/work-intents/admit", json=_intent())
    blocker = {"blocker_type": "gate_error", "reason": "bind awaits canonical target",
               "unblock_action": "declare target", "owner": "alfred-owner",
               "next_check_at": 1234567890}
    assert client.post(f"/work-intents/{OP_ID}/block", json=blocker).status_code == 200
    before = client.get(f"/work-intents/{OP_ID}").json()

    replay = client.post("/work-intents/admit", json=_intent(
        target_repo="owner/alfred", target_issue=405))

    assert replay.status_code == 201
    assert replay.json()["state"] == "BLOCKED"
    assert replay.json()["blocker"] == before["blocker"]
    assert replay.json()["target_issue"] == 405
    with sqlite3.connect(queue.db_path) as conn:
        rows = conn.execute("SELECT fields FROM task_exec_events WHERE task_id=? "
                            "AND event='work_intent_target_set'", ("work-intent:" + OP_ID,)).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][0])["target_issue"] == 405
    assert client.post("/work-intents/admit", json=_intent(
        target_repo="owner/alfred", target_issue=405)).status_code == 201
    with sqlite3.connect(queue.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_exec_events WHERE task_id=? "
                            "AND event='work_intent_target_set'",
                            ("work-intent:" + OP_ID,)).fetchone()[0] == 1
    _task(queue, "test-405", task_type="test", status="completed")

    bound = _bind(client, "test-405")

    assert bound.status_code == 200
    assert bound.json()["state"] == "SUCCESS"
    assert bound.json()["task_ids"] == ["test-405"]
    assert bound.json()["decision_authority"] == "agent_crew:task_result"
    assert queue.get_task("test-405").context["issue"] == 405


@pytest.mark.parametrize("target, task_kwargs, detail", [
    ({}, {}, "issue"),
    ({"target_repo": "owner/alfred", "target_issue": 406}, {}, "issue"),
    ({"target_repo": "owner/other", "target_issue": 405}, {}, "repo"),
    ({"target_repo": "owner/alfred", "target_issue": 405}, {"project": "other"}, "project"),
    ({"target_repo": "owner/alfred", "target_issue": 405}, {"status": "failed"}, "active"),
    ({"target_repo": "owner/alfred", "target_issue": 405}, {"status": "cancelled"}, "active"),
])
def test_mismatch_or_unsuccessful_task_still_refused(crew, target, task_kwargs, detail):
    client, queue = crew
    client.post("/work-intents/admit", json=_intent(**target))
    _task(queue, "impl-405", **task_kwargs)

    response = _bind(client, "impl-405")

    assert response.status_code == 409
    assert detail in response.json()["detail"]
    assert client.get(f"/work-intents/{OP_ID}").json()["task_ids"] == []


def test_replay_conflicting_target_is_conflict_and_identity_drift_is_noop(crew):
    client, _ = crew
    original = client.post("/work-intents/admit", json=_intent(
        target_repo="owner/alfred", target_issue=405)).json()

    conflict = client.post("/work-intents/admit", json=_intent(
        target_repo="owner/alfred", target_issue=406))
    drift = client.post("/work-intents/admit", json=_intent(
        issue=45, target_repo="owner/alfred", target_issue=405))

    assert conflict.status_code == 409
    assert drift.status_code == 201
    assert drift.json() == original


def test_identity_drift_cannot_set_missing_target(crew):
    client, _ = crew
    original = client.post("/work-intents/admit", json=_intent()).json()
    replay = client.post("/work-intents/admit", json=_intent(
        issue=45, target_repo="owner/alfred", target_issue=405))
    assert replay.status_code == 201
    assert replay.json() == original
    assert replay.json()["target_issue"] is None


@pytest.mark.parametrize("terminal", ["SUCCESS", "REJECTED", "SUPERSEDED"])
def test_terminal_intent_does_not_gain_target_on_replay(crew, terminal):
    client, queue = crew
    client.post("/work-intents/admit", json=_intent())
    with sqlite3.connect(queue.db_path) as conn:
        conn.execute("UPDATE work_intents SET state=? WHERE external_operation_id=?",
                     (terminal, OP_ID))
    replay = client.post("/work-intents/admit", json=_intent(
        target_repo="owner/alfred", target_issue=405))
    assert replay.status_code == 201
    assert replay.json()["state"] == terminal
    assert replay.json()["target_issue"] is None


def test_legacy_row_migrates_without_changing_blocked_state(tmp_path):
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    queue.admit_work_intent(_intent())
    queue.decide_work_intent(OP_ID, "BLOCKED", blocker={
        "blocker_type": "gate_error", "reason": "await target",
        "unblock_action": "declare target", "owner": "alfred-owner",
        "next_check_at": 1234567890,
    })
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE work_intents DROP COLUMN target_repo")
        conn.execute("ALTER TABLE work_intents DROP COLUMN target_issue")

    migrated = TaskQueue(db).get_work_intent(OP_ID)
    assert migrated["state"] == "BLOCKED"
    assert migrated["issue"] == 44
    assert (migrated["target_repo"], migrated["target_issue"]) == (None, None)


@pytest.mark.parametrize("target", [
    {"target_repo": "owner/alfred"},
    {"target_issue": 405},
    {"target_repo": "", "target_issue": 405},
    {"target_repo": "owner/alfred", "target_issue": True},
    {"target_repo": "owner/alfred", "target_issue": 0},
    {"target_repo": "owner/alfred", "target_issue": "405"},
])
def test_admit_rejects_half_or_invalid_target(crew, target):
    client, _ = crew
    assert client.post("/work-intents/admit", json=_intent(**target)).status_code == 422
