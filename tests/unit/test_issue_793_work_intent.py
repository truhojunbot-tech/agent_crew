"""#793: work admission is durable, bound to tasks, and watched after ACK."""

import json
import sqlite3

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from agent_crew import cli
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.pipeline import _carry_lineage_intent
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _app(db, notices):
    return create_app(str(db), project="agent_crew", pane_map={},
                      watchdog_disabled=True, anomaly_disabled=True,
                      identity_required=False,
                      coordinator_notify_fn=lambda message: notices.append(message) or True)


def _intent(op="directive-1", **extra):
    return {"external_operation_id": op, "project": "agent_crew",
            "repo": "owner/repo", "issue": 793, "coordinator_identity": "coordinator",
            "coordinator_session": "session-1", "priority": 3, "risk_tier": 2,
            "execution_slo_s": 10, **extra}


def _task(task_id="impl-793"):
    return {"task_id": task_id, "task_type": "implement", "description": "Implement #793",
            "branch": "agent/793-work-intent-liveness", "project": "agent_crew",
            "context": {"issue": 793, "risk_tier": 2}}


def test_ack_stays_nonterminal_and_restart_deduplicates(tmp_path):
    db = tmp_path / "tasks.db"
    with TestClient(_app(db, [])) as client:
        first = client.post("/work-intents/admit", json=_intent()).json()
        assert first["state"] == "ADMITTED"
        assert first["task_ids"] == []
    with TestClient(_app(db, [])) as client:
        again = client.post("/work-intents/admit", json=_intent()).json()
        assert again == first
        assert client.get("/work-intents/directive-1").json() == first


def test_atomic_first_task_and_existing_bind(tmp_path):
    db = tmp_path / "tasks.db"
    with TestClient(_app(db, [])) as client:
        response = client.post("/work-intents/admit", json=_intent(task=_task()))
        assert response.status_code == 201
        assert response.json()["state"] == "BOUND"
        assert response.json()["task_ids"] == ["impl-793"]
        duplicate = client.post("/work-intents/admit", json=_intent(task=_task("different")))
        assert duplicate.json()["task_ids"] == ["impl-793"]
        queue = TaskQueue(str(db))
        assert queue.get_task("different") is None
        assert queue.get_task("impl-793").context["external_operation_id"] == "directive-1"
        queue.enqueue(TaskRequest(**_task("review-793") | {"task_type": "review"}),
                      ingress="http.tasks")
        bound = client.post("/work-intents/directive-1/bind", json={"task_id": "review-793"})
        assert bound.status_code == 200
        assert bound.json()["task_ids"] == ["impl-793", "review-793"]
        assert queue.get_task("review-793").context["external_operation_id"] == "directive-1"


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "timed_out"])
def test_terminal_task_cannot_bind_unstarted_intent(tmp_path, status):
    db = tmp_path / "tasks.db"
    notices = []
    with TestClient(_app(db, notices)) as client:
        admitted = client.post("/work-intents/admit", json=_intent()).json()
        queue = TaskQueue(str(db))
        queue.enqueue(TaskRequest(**_task()), ingress="http.tasks")
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE tasks SET status=? WHERE task_id='impl-793'", (status,))

        response = client.post("/work-intents/directive-1/bind", json={"task_id": "impl-793"})
        assert response.status_code == 409
        assert status in response.json()["detail"]
        intent = client.get("/work-intents/directive-1").json()
        assert intent["state"] == "ADMITTED"
        assert intent["task_ids"] == []
        assert queue.get_task("impl-793").context.get("external_operation_id") is None

        client.app.state.watchdog_tick(admitted["received_at"] + 11)
        assert client.get("/work-intents/directive-1").json()["state"] == "EXECUTION_STALLED"
        assert len(notices) == 1


def test_first_task_rolls_back_when_intent_insert_fails(tmp_path):
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TRIGGER reject_intent BEFORE INSERT ON work_intents "
                     "BEGIN SELECT RAISE(ABORT, 'intent insert refused'); END")
    with pytest.raises(sqlite3.IntegrityError):
        queue.admit_work_intent(_intent(task=_task()))
    assert queue.get_task("impl-793") is None
    assert queue.get_work_intent("directive-1") is None


def test_completed_bound_test_terminalizes_intent_without_manual_transition(tmp_path):
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    test_task = _task("test-189a1205") | {"task_type": "test",
                                          "description": "Read-only verification"}
    admitted = queue.admit_work_intent(_intent("alpha5599:6283277797", task=test_task))
    assert admitted["state"] == "BOUND"
    queue.submit_result(
        "test-189a1205",
        TaskResult(task_id="test-189a1205", status="completed",
                   summary="Read-only verification completed successfully."),
    )
    terminal = queue.get_work_intent("alpha5599:6283277797")
    assert terminal["state"] == "SUCCESS"
    assert terminal["task_ids"] == ["test-189a1205"]
    assert terminal["decision_authority"] == "agent_crew:task_result"


def test_completed_implement_does_not_terminalize_before_successor(tmp_path):
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    admitted = queue.admit_work_intent(_intent(task=_task()))
    assert admitted["state"] == "BOUND"
    queue.submit_result(
        "impl-793",
        TaskResult(task_id="impl-793", status="completed",
                   summary="Implementation completed; review successor is still required."),
    )
    assert queue.get_work_intent("directive-1")["state"] == "BOUND"


@pytest.mark.parametrize("order", [("test-793", "review-793"), ("review-793", "test-793")])
def test_all_bound_completed_terminalizes_in_either_order(tmp_path, order):
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    queue.admit_work_intent(_intent(task=_task("test-793") | {"task_type": "test"}))
    queue.enqueue(TaskRequest(**_task("review-793") | {"task_type": "review"}),
                  ingress="http.tasks")
    queue.bind_work_intent("directive-1", "review-793")
    first, last = order
    queue.submit_result(first, TaskResult(task_id=first, status="completed", summary="done"))
    # One bound task is still pending: the operation is not done, whichever finished.
    assert queue.get_work_intent("directive-1")["state"] == "BOUND"
    queue.submit_result(last, TaskResult(task_id=last, status="completed", summary="done"))
    terminal = queue.get_work_intent("directive-1")
    assert terminal["state"] == "SUCCESS"
    assert terminal["decision_authority"] == "agent_crew:task_result"


def test_completed_review_does_not_terminalize_without_test(tmp_path):
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    queue.admit_work_intent(_intent(task=_task()))
    queue.enqueue(TaskRequest(**_task("review-793") | {"task_type": "review"}),
                  ingress="http.tasks")
    queue.bind_work_intent("directive-1", "review-793")
    for task_id in ("impl-793", "review-793"):
        queue.submit_result(task_id, TaskResult(task_id=task_id, status="completed",
                                                summary="done"))
    # Every bound task is completed, but no test ran: review is not the terminal role.
    assert queue.get_work_intent("directive-1")["state"] == "BOUND"


def test_terminal_success_rejects_result_correction(tmp_path):
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    queue.admit_work_intent(_intent(task=_task("test-793") | {"task_type": "test"}))
    queue.submit_result("test-793", TaskResult(task_id="test-793", status="completed",
                                               summary="passed"))
    assert queue.get_work_intent("directive-1")["state"] == "SUCCESS"
    with pytest.raises(ValueError, match="terminal SUCCESS"):
        queue.submit_result("test-793", TaskResult(
            task_id="test-793", status="failed",
            summary="correction: suite actually failed"))
    assert queue.get_task("test-793").status == "completed"
    assert queue.get_work_intent("directive-1")["state"] == "SUCCESS"


def test_request_changes_review_keeps_intent_bound_for_fix_successor(tmp_path):
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    queue.admit_work_intent(_intent(task=_task("test-793") | {"task_type": "test"}))
    queue.enqueue(TaskRequest(**_task("review-793") | {"task_type": "review"}),
                  ingress="http.tasks")
    queue.bind_work_intent("directive-1", "review-793")
    queue.submit_result("test-793", TaskResult(
        task_id="test-793", status="completed", summary="test passed"))
    queue.submit_result("review-793", TaskResult(
        task_id="review-793", status="completed", summary="changes required",
        verdict="request_changes", findings=["fix the terminal-state edge case"]))
    assert queue.get_work_intent("directive-1")["state"] == "BOUND"
    queue.enqueue(TaskRequest(**_task("fix-793")), ingress="http.tasks")
    bound = queue.bind_work_intent("directive-1", "fix-793")
    assert "fix-793" in bound["task_ids"]
    assert bound["state"] == "BOUND"


def test_intent_update_failure_rolls_back_task_completion(tmp_path):
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    queue.admit_work_intent(_intent(task=_task("test-793") | {"task_type": "test"}))
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TRIGGER reject_success BEFORE UPDATE OF state ON work_intents "
                     "WHEN NEW.state='SUCCESS' "
                     "BEGIN SELECT RAISE(ABORT, 'intent update refused'); END")
    with pytest.raises(sqlite3.IntegrityError):
        queue.submit_result("test-793", TaskResult(task_id="test-793", status="completed",
                                                   summary="passed"))
    # Same transaction: the task result is not durable without the intent update.
    assert queue.get_task("test-793").status == "pending"
    assert queue.get_work_intent("directive-1")["state"] == "BOUND"


def test_successful_intent_rejects_late_rebind(tmp_path):
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    test_task = _task("test-793") | {"task_type": "test", "description": "Terminal test"}
    queue.admit_work_intent(_intent(task=test_task))
    queue.submit_result(
        "test-793",
        TaskResult(task_id="test-793", status="completed",
                   summary="Terminal test completed successfully."),
    )
    queue.enqueue(TaskRequest(**_task("late-review") | {"task_type": "review"}),
                  ingress="http.tasks")
    with pytest.raises(ValueError, match="terminal"):
        queue.bind_work_intent("directive-1", "late-review")


def test_block_and_terminal_decisions_are_structured(tmp_path):
    with TestClient(_app(tmp_path / "tasks.db", [])) as client:
        client.post("/work-intents/admit", json=_intent())
        assert client.post("/work-intents/directive-1/block", json={"reason": "waiting"}).status_code == 422
        blocker = {"blocker_type": "dependency", "reason": "upstream schema pending",
                   "unblock_action": "merge schema", "owner": "owner/repo", "next_check_at": 9000.0}
        blocked = client.post("/work-intents/directive-1/block", json=blocker).json()
        assert blocked["state"] == "BLOCKED" and blocked["blocker"] == blocker
        assert client.post("/work-intents/directive-1/reject", json={"reason": "duplicate"}).status_code == 422
        rejected = client.post("/work-intents/directive-1/reject",
                               json={"reason": "duplicate", "authority": "owner"}).json()
        assert rejected["state"] == "REJECTED"
        assert rejected["decision_reason"] == "duplicate"
        assert rejected["decision_authority"] == "owner"
        client.post("/work-intents/admit", json=_intent("directive-2"))
        superseded = client.post("/work-intents/directive-2/supersede",
                                 json={"reason": "replaced", "authority": "owner"}).json()
        assert superseded["state"] == "SUPERSEDED"


def test_unbound_slo_notices_once_then_escalates_and_survives_restart(tmp_path):
    db = tmp_path / "tasks.db"
    notices = []
    with TestClient(_app(db, notices)) as client:
        admitted = client.post("/work-intents/admit", json=_intent()).json()
        app = client.app
        app.state.watchdog_tick(admitted["received_at"] + 11)
        stalled = client.get("/work-intents/directive-1").json()
        assert stalled["state"] == "EXECUTION_STALLED"
        assert len(notices) == 1 and "work_intent_stalled" in notices[0]
        app.state.watchdog_tick(stalled["next_check_at"] - 1)
        assert len(notices) == 1
        app.state.watchdog_tick(stalled["next_check_at"] + 1)
        assert len(notices) == 1
        assert client.get("/work-intents", params={"state": "EXECUTION_STALLED"}).json()[0]["escalated"]
        assert client.get("/health").json()["work_intents"]["EXECUTION_STALLED"] == 1
        assert client.get("/health").json()["work_intents"]["EXECUTION_STALLED_ESCALATED"] == 1
    with TestClient(_app(db, notices)) as client:
        assert client.get("/work-intents/directive-1").json()["escalated"]
        client.app.state.watchdog_tick(stalled["next_check_at"] + 2)
        assert len(notices) == 1
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM task_exec_events WHERE event='work_intent_escalated'").fetchone()[0] == 1


def test_blocked_does_not_notify_before_check_and_successor_lineage(tmp_path):
    db = tmp_path / "tasks.db"
    notices = []
    with TestClient(_app(db, notices)) as client:
        client.post("/work-intents/admit", json=_intent(task=_task()))
        queue = TaskQueue(str(db))
        for kind in ("review", "test", "implement"):
            child = TaskRequest(task_id=f"successor-{kind}", task_type=kind,
                                description=f"Successor {kind}", branch="agent/793-work-intent-liveness",
                                project="agent_crew", context={"prev_task_id": "impl-793",
                                                               "risk_tier": 2,
                                                               "external_operation_id": "directive-1"})
            queue.enqueue(child, ingress="cascade.fix" if kind == "implement" else "cascade.review")
        assert set(queue.get_work_intent("directive-1")["task_ids"]) == {
            "impl-793", "successor-review", "successor-test", "successor-implement"}
    with TestClient(_app(tmp_path / "blocked.db", notices)) as client:
        row = client.post("/work-intents/admit", json=_intent()).json()
        client.post("/work-intents/directive-1/block", json={
            "blocker_type": "dependency", "reason": "waiting", "unblock_action": "merge",
            "owner": "coordinator", "next_check_at": row["received_at"] + 100})
        client.app.state.watchdog_tick(row["received_at"] + 50)
        assert notices == []
        client.app.state.watchdog_tick(row["received_at"] + 101)
        assert len(notices) == 1 and "work_intent_blocked_due" in notices[0]


def test_pipeline_carries_operation_id_across_all_successor_roles():
    parent = TaskRequest(**_task())
    parent.context["external_operation_id"] = "directive-1"
    for role in ("implementer", "reviewer", "tester"):
        context = {"prev_task_id": parent.task_id}
        _carry_lineage_intent(context, parent, role)
        assert context["external_operation_id"] == "directive-1"


def test_intent_cli_routes_to_project_server(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_read_state", lambda base, project: {"port": 8105})
    monkeypatch.setattr(cli, "_intent_http", lambda *args: calls.append(args) or {"state": "BOUND"})
    runner = CliRunner()
    response = runner.invoke(cli.crew, ["intent", "--project", "agent_crew",
                                        "admit", json.dumps(_intent())])
    assert response.exit_code == 0, response.output
    assert calls[-1] == ("agent_crew", 8105, "admit", _intent())
    response = runner.invoke(cli.crew, ["intent", "--project", "agent_crew",
                                        "bind", "directive-1", "impl-793"])
    assert response.exit_code == 0, response.output
    assert calls[-1] == ("agent_crew", 8105, "directive-1/bind", {"task_id": "impl-793"})
