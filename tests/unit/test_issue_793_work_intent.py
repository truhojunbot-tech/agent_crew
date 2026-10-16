"""#793: work admission is durable, bound to tasks, and watched after ACK."""

import json
import sqlite3

from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest
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
