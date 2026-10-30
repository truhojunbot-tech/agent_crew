"""#959: a STOP-suppressed test merge remains owed until replay."""

import sqlite3
import time
import pytest
from fastapi.testclient import TestClient

from agent_crew import github, pause
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


@pytest.fixture
def merge_case(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setattr(pause, "GLOBAL_PAUSE_FILE", str(tmp_path / "global-pause.json"))
    # The resume authority is covered elsewhere; this fixture isolates merge replay.
    monkeypatch.setattr(TaskQueue, "_authorize_legacy_loosening_in_txn",
                        lambda *a, **k: None)
    project = tmp_path / "project"
    project.mkdir()
    queue = TaskQueue(str(project / "tasks.db"))
    head = {"sha": "a" * 40, "state": "open"}
    merges, notices = [], []
    pr = 959
    for task_id, task_type, ctx in (
        ("impl-959", "implement", {"risk_tier": 1}),
        ("review-959", "review", {"risk_tier": 1, "prev_task_id": "impl-959",
                                  "pr_number": pr, "repo": "owner/repo", "reviewed_sha": head["sha"]}),
        ("test-959", "test", {"risk_tier": 1, "prev_task_id": "review-959",
                              "pr_number": pr, "repo": "owner/repo", "reviewed_sha": head["sha"]}),
    ):
        queue.enqueue(TaskRequest(task_id=task_id, task_type=task_type,
                                  description=task_type, branch="fix/959",
                                  project="project", context=ctx))
    queue.submit_result("review-959", TaskResult(task_id="review-959", status="completed",
                                                 summary="approved", verdict="approve",
                                                 pr_number=pr))
    monkeypatch.setattr(github, "pr_state", lambda *a, **k: head["state"])
    monkeypatch.setattr(github, "pr_head_sha", lambda *a, **k: head["sha"])
    monkeypatch.setattr(github, "independent_review_for_head",
                        lambda *a, **k: (head["sha"], "claude", "review-959", "ok"))
    monkeypatch.setattr(github, "publish_independent_review_status", lambda *a, **k: True)
    monkeypatch.setattr(github, "independent_review_succeeded", lambda *a, **k: True)
    monkeypatch.setattr(github, "head_checks_state", lambda *a, **k: ("green", ""))
    monkeypatch.setattr(github, "merge_pr", lambda *a, **k: merges.append(1) or True)
    monkeypatch.setattr("agent_crew.conformance_gate._conformance_gate_allows_merge",
                        lambda *a, **k: True)
    monkeypatch.setattr("agent_crew.conformance_gate.test_pass_for_head",
                        lambda *a, **k: True)
    app = create_app(str(project / "tasks.db"), project="project", pane_map={},
                     watchdog_disabled=True, anomaly_disabled=True,
                     coordinator_notify_fn=lambda message: notices.append(message) or True)
    return queue, app, head, merges, notices


def _submit_with_stop_race(client, queue, monkeypatch):
    original = TaskQueue.external_op_reserve
    flipped = False

    def stop_before_merge(self, key, pr_number=None):
        nonlocal flipped
        if key == "merge:pr:959" and not flipped:
            flipped = True
            self.set_stop_epoch(True)
        return original(self, key, pr_number)

    monkeypatch.setattr(TaskQueue, "external_op_reserve", stop_before_merge)
    response = client.post("/tasks/test-959/result", json={
        "task_id": "test-959", "status": "completed", "summary": "PASS",
        "pr_number": 959, "error_info": {"script_test": {
            "outcome": "PASSED", "head_sha": "a" * 40}},
    }, headers={"X-Agent-Crew-Project": "project"})
    assert response.status_code == 200, response.text
    assert flipped
    assert queue.get_stop_epoch()["paused"]


def _resume(queue):
    epoch = queue.get_stop_epoch()["epoch"]
    assert queue.resume_stop(generation=epoch + 1, who="owner:test",
                             decision_id="D-959")["resumed"]


def test_paused_merge_replays_once(merge_case, monkeypatch):
    queue, app, head, merges, notices = merge_case
    with TestClient(app) as client:
        _submit_with_stop_race(client, queue, monkeypatch)
        assert merges == []
        assert queue.outbox_get("test-959")["state"] == "pending"
        assert queue.get_task_context("test-959")["suppressed_merge"] == {
            "pr_number": 959, "head_sha": "a" * 40,
            "review_task_id": "review-959", "repo": "owner/repo"}
        # An owed merge survives the ordinary cascade replay age cap.
        with sqlite3.connect(queue._db_path) as db:
            db.execute("UPDATE cascade_outbox SET created_at=? WHERE parent_task_id='test-959'",
                       (time.time() - 90000,))
        _resume(queue)
        assert client.post("/admin/replay-suppressed").status_code == 200
        assert merges == [1]
        assert queue.outbox_get("test-959")["state"] == "applied"
        assert client.post("/admin/replay-suppressed").status_code == 200
        assert merges == [1]


def test_head_move_refuses_with_one_notice(merge_case, monkeypatch):
    queue, app, head, merges, notices = merge_case
    with TestClient(app) as client:
        _submit_with_stop_race(client, queue, monkeypatch)
        head["sha"] = "b" * 40
        _resume(queue)
        client.post("/admin/replay-suppressed")
        client.post("/admin/replay-suppressed")
    assert merges == []
    assert sum("merge_replay_refused" in notice for notice in notices) == 1
    assert len(notices) == 1
    assert queue.outbox_get("test-959")["state"] == "applied"


def test_hand_merged_pr_finishes_without_merge_call(merge_case, monkeypatch):
    queue, app, head, merges, notices = merge_case
    with TestClient(app) as client:
        _submit_with_stop_race(client, queue, monkeypatch)
        head["state"] = "merged"
        _resume(queue)
        client.post("/admin/replay-suppressed")
    assert merges == []
    assert queue.outbox_get("test-959")["state"] == "applied"
    assert queue.external_op_get("merge:pr:959")["state"] == "done"


def test_closed_pr_refuses_with_one_typed_notice(merge_case, monkeypatch):
    queue, app, head, merges, notices = merge_case
    with TestClient(app) as client:
        _submit_with_stop_race(client, queue, monkeypatch)
        head["state"] = "closed"
        _resume(queue)
        client.post("/admin/replay-suppressed")
        client.post("/admin/replay-suppressed")
    assert merges == []
    assert len(notices) == 1
    assert "reason_code=pr_closed" in notices[0]
    assert queue.outbox_get("test-959")["state"] == "applied"


def test_head_lookup_failure_still_records_owed_merge(merge_case, monkeypatch):
    queue, app, head, merges, notices = merge_case
    original = github.pr_head_sha
    calls = 0

    def flaky_head(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("head lookup unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(github, "pr_head_sha", flaky_head)
    with TestClient(app) as client:
        _submit_with_stop_race(client, queue, monkeypatch)
        assert queue.outbox_get("test-959")["state"] == "pending"
        assert queue.get_task_context("test-959")["suppressed_merge"]["head_sha"] == ""
        _resume(queue)
        client.post("/admin/replay-suppressed")
    assert merges == [1]


def test_merge_gate_refusal_notifies_once(merge_case, monkeypatch):
    queue, app, head, merges, notices = merge_case
    with TestClient(app) as client:
        _submit_with_stop_race(client, queue, monkeypatch)
        monkeypatch.setattr("agent_crew.conformance_gate._conformance_gate_allows_merge",
                            lambda *a, **k: False)
        _resume(queue)
        client.post("/admin/replay-suppressed")
        client.post("/admin/replay-suppressed")
    assert merges == []
    assert len(notices) == 1
    assert "reason_code=merge_gate_refused" in notices[0]
    assert queue.outbox_get("test-959")["state"] == "applied"


def test_public_task_cannot_claim_owed_merge(merge_case):
    queue, app, head, merges, notices = merge_case
    with TestClient(app) as client:
        response = client.post("/tasks", json={
            "task_id": "impl-forged-owed", "task_type": "implement",
            "description": "task", "branch": "fix/959", "project": "project",
            "context": {"risk_tier": 1, "suppressed_merge": {"pr_number": 959}},
        }, headers={"X-Agent-Crew-Project": "project"})
    assert response.status_code == 422
    assert response.json()["detail"] == "reserved_context_key:suppressed_merge"
