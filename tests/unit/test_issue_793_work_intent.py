"""#793: work admission is durable, bound to tasks, and watched after ACK."""

import json
import sqlite3

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from agent_crew import cli
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.pipeline import _carry_lineage_intent
from agent_crew.queue import _CEA_SYSTEM_SUCCESSOR_PROVENANCE, TaskQueue
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


@pytest.mark.parametrize("status", ["failed", "cancelled", "timed_out"])
def test_unsuccessful_task_cannot_bind_unstarted_intent(tmp_path, status):
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


def _lineage_task(task_id, task_type, prev_task_id, **context):
    return _task(task_id) | {"task_type": task_type,
                             "context": {"issue": 793, "risk_tier": 2,
                                         "prev_task_id": prev_task_id,
                                         "external_operation_id": "directive-1", **context}}


def _complete(queue, task_id, **fields):
    queue.submit_result(task_id, TaskResult(task_id=task_id, status="completed",
                                            summary=f"{task_id} done", **fields))


def _dispatched_test(queue, task_id, scope="full_suite", digest="policy-h1"):
    """What the dispatcher persists for a test it actually sent out (#278):
    the effective test selection, from the admission receipt and the policy
    in force -- never from the request context."""
    queue.record_attribution(task_id, project="agent_crew", role="tester", task_type="test")
    queue.record_test_economics(task_id, effective_test_scope=scope,
                                test_scope_source="builtin", test_scope_hash=digest)


def _system_retry(queue, task_id, parent_id, task_type, **context):
    """Enqueue a retry the way server._auto_retry_failed_task does: a
    deterministic id, ``original_task_id`` lineage, and the in-process
    provenance token without which admission drops that key."""
    queue.enqueue(TaskRequest(**_lineage_task(task_id, task_type, parent_id,
                                              original_task_id=parent_id,
                                              retry_of=parent_id,
                                              retry_attempt=1, **context)),
                  ingress="retry.failed_task",
                  _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)


def test_corrected_review_lineage_reaches_success(tmp_path):
    """Review of PR #817 at acff035: a resolved request_changes must not veto SUCCESS."""
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    queue.admit_work_intent(_intent(task=_task()))
    # Pipeline lineage: impl -> review-1 -> fix -> review-2 -> test (prev_task_id chain).
    for spec in (_lineage_task("review-1", "review", "impl-793"),
                 _lineage_task("fix-1", "implement", "review-1", fix_round=1),
                 _lineage_task("review-2", "review", "fix-1"),
                 _lineage_task("test-793", "test", "review-2")):
        queue.enqueue(TaskRequest(**spec), ingress="http.tasks")
    assert set(queue.get_work_intent("directive-1")["task_ids"]) == {
        "impl-793", "review-1", "fix-1", "review-2", "test-793"}
    _complete(queue, "impl-793")
    _complete(queue, "review-1", verdict="request_changes", findings=["veto bug"])
    _complete(queue, "fix-1")
    # Fix landed but its re-review has not spoken: the request_changes is still open.
    assert queue.get_work_intent("directive-1")["state"] == "BOUND"
    _complete(queue, "review-2", verdict="approve")
    assert queue.get_work_intent("directive-1")["state"] == "BOUND"
    _complete(queue, "test-793")
    terminal = queue.get_work_intent("directive-1")
    assert terminal["state"] == "SUCCESS"
    assert terminal["decision_authority"] == "agent_crew:task_result"


def test_second_request_changes_in_lineage_still_vetoes_success(tmp_path):
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    queue.admit_work_intent(_intent(task=_task()))
    for spec in (_lineage_task("review-1", "review", "impl-793"),
                 _lineage_task("fix-1", "implement", "review-1", fix_round=1),
                 _lineage_task("review-2", "review", "fix-1"),
                 _lineage_task("test-793", "test", "review-1")):
        queue.enqueue(TaskRequest(**spec), ingress="http.tasks")
    _complete(queue, "impl-793")
    _complete(queue, "test-793")
    _complete(queue, "review-1", verdict="request_changes", findings=["first"])
    _complete(queue, "fix-1")
    # review-1 is resolved by review-2, but review-2 itself requests changes again.
    _complete(queue, "review-2", verdict="request_changes", findings=["second"])
    assert queue.get_work_intent("directive-1")["state"] == "BOUND"
    queue.enqueue(TaskRequest(**_lineage_task("fix-2", "implement", "review-2", fix_round=2)),
                  ingress="http.tasks")
    queue.enqueue(TaskRequest(**_lineage_task("review-3", "review", "fix-2")),
                  ingress="http.tasks")
    _complete(queue, "fix-2")
    assert queue.get_work_intent("directive-1")["state"] == "BOUND"
    _complete(queue, "review-3", verdict="approve")
    assert queue.get_work_intent("directive-1")["state"] == "SUCCESS"


def test_direct_rereview_without_fix_does_not_clear_request_changes(tmp_path):
    """Review of PR #817 at 7b7f11e: an approving re-review resolves a
    request_changes only through a completed bound implement fix. A review
    that descends directly from the vetoing review carries no correction, so
    the veto stands and the intent must not terminalize."""
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    queue.admit_work_intent(_intent(task=_task()))
    for spec in (_lineage_task("review-1", "review", "impl-793"),
                 _lineage_task("review-2", "review", "review-1"),
                 _lineage_task("test-793", "test", "review-2")):
        queue.enqueue(TaskRequest(**spec), ingress="http.tasks")
    _complete(queue, "impl-793")
    _complete(queue, "review-1", verdict="request_changes", findings=["veto bug"])
    _complete(queue, "review-2", verdict="approve")
    _complete(queue, "test-793")
    intent = queue.get_work_intent("directive-1")
    assert intent["state"] == "BOUND"
    assert "fix" not in " ".join(intent["task_ids"])  # no implement fix was ever bound


def _fail(queue, task_id, status="failed"):
    queue.submit_result(task_id, TaskResult(task_id=task_id, status=status,
                                            summary=f"{task_id} {status}"))


def _assert_blocked_on(queue, *failed):
    """#823: an unsuperseded terminal failure with no active bound task leaves
    the intent BLOCKED with a structured blocker -- never SUCCESS, and never a
    silent permanent BOUND. ``failed`` lists the expected ``task=status`` pairs."""
    intent = queue.get_work_intent("directive-1")
    assert intent["state"] == "BLOCKED"
    assert intent["state"] != "SUCCESS"
    blocker = intent["blocker"]
    assert blocker["blocker_type"] == "bound_tasks_unsuccessful"
    for pair in failed:
        assert pair in blocker["reason"]
    assert blocker["unblock_action"]
    assert blocker["owner"] == "coordinator"
    assert blocker["next_check_at"] is not None
    assert intent["next_check_at"] == blocker["next_check_at"]
    assert intent["decision_reason"] == blocker["reason"]
    assert "superseded" not in intent["decision_reason"]
    return intent


def test_rereview_after_incomplete_fix_does_not_clear_request_changes(tmp_path):
    """A fix task in the chain only counts as correction once it is completed."""
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    queue.admit_work_intent(_intent(task=_task()))
    for spec in (_lineage_task("review-1", "review", "impl-793"),
                 _lineage_task("fix-1", "implement", "review-1", fix_round=1),
                 _lineage_task("review-2", "review", "fix-1"),
                 _lineage_task("test-793", "test", "review-1")):
        queue.enqueue(TaskRequest(**spec), ingress="http.tasks")
    _complete(queue, "impl-793")
    _complete(queue, "test-793")
    _complete(queue, "review-1", verdict="request_changes", findings=["veto bug"])
    queue.submit_result("fix-1", TaskResult(task_id="fix-1", status="failed",
                                            summary="fix-1 could not land"))
    _complete(queue, "review-2", verdict="approve")
    _assert_blocked_on(queue, "fix-1=failed")




def test_terminal_unsuccessful_bound_task_becomes_structured_blocked(tmp_path):
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    queue.admit_work_intent(_intent(task=_task("test-793") | {"task_type": "test"}))
    _fail(queue, "test-793", status="timed_out")
    intent = queue.get_work_intent("directive-1")
    assert intent["state"] == "BLOCKED"
    assert intent["blocker"]["blocker_type"] == "bound_tasks_unsuccessful"
    assert "test-793=timed_out" in intent["blocker"]["reason"]
    assert intent["blocker"]["unblock_action"]
    assert intent["blocker"]["owner"]
    assert intent["next_check_at"] == intent["blocker"]["next_check_at"]


def test_binding_retry_reopens_blocked_intent_to_bound(tmp_path):
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    queue.admit_work_intent(_intent(task=_task("test-793") | {"task_type": "test"}))
    _fail(queue, "test-793", status="failed")
    assert queue.get_work_intent("directive-1")["state"] == "BLOCKED"
    _system_retry(queue, "test-793-retry", "test-793", "test")
    rebound = queue.bind_work_intent("directive-1", "test-793-retry")
    assert rebound["state"] == "BOUND"
    assert rebound["blocker"] is None


def test_failed_bound_task_with_active_sibling_stays_bound(tmp_path):
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    queue.admit_work_intent(_intent(task=_task("test-793") | {"task_type": "test"}))
    queue.enqueue(TaskRequest(**_lineage_task("fix-793", "implement", "test-793")),
                  ingress="http.tasks")
    queue.bind_work_intent("directive-1", "fix-793")
    _fail(queue, "test-793", status="failed")
    assert queue.get_task("fix-793").status == "pending"
    intent = queue.get_work_intent("directive-1")
    assert intent["state"] == "BOUND"
    assert intent["blocker"] is None


def test_timed_out_fix_superseded_by_completed_retry_reaches_success(tmp_path):
    """#821: a dead fix round stays bound but no longer vetoes SUCCESS once
    its system retry (trusted ``original_task_id`` provenance) completed,
    was approved and tested."""
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    queue.admit_work_intent(_intent(task=_task()))
    for spec in (_lineage_task("review-1", "review", "impl-793"),
                 _lineage_task("fix-1", "implement", "review-1", fix_round=1,
                               review_findings=["veto bug"])):
        queue.enqueue(TaskRequest(**spec), ingress="http.tasks")
    _complete(queue, "impl-793")
    _complete(queue, "review-1", verdict="request_changes", findings=["veto bug"])
    _fail(queue, "fix-1", status="timed_out")
    _system_retry(queue, "fix-1-retry", "fix-1", "implement", fix_round=1,
                  review_findings=["veto bug"])
    for spec in (_lineage_task("review-2", "review", "fix-1-retry"),
                 _lineage_task("test-793", "test", "review-2")):
        queue.enqueue(TaskRequest(**spec), ingress="http.tasks")
    _complete(queue, "fix-1-retry")
    _complete(queue, "review-2", verdict="approve")
    assert queue.get_work_intent("directive-1")["state"] == "BOUND"
    _complete(queue, "test-793")
    terminal = queue.get_work_intent("directive-1")
    assert terminal["state"] == "SUCCESS"
    assert terminal["decision_authority"] == "agent_crew:task_result"
    assert "fix-1" in terminal["task_ids"]  # audit trail keeps the dead round
    assert "superseded" in terminal["decision_reason"]
    assert queue.get_task("fix-1").status == "timed_out"


def test_only_fix_failed_does_not_reach_success(tmp_path):
    """#821: a dead fix with no completed successor still blocks SUCCESS."""
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    queue.admit_work_intent(_intent(task=_task()))
    for spec in (_lineage_task("review-1", "review", "impl-793"),
                 _lineage_task("fix-1", "implement", "review-1", fix_round=1),
                 _lineage_task("test-793", "test", "review-1")):
        queue.enqueue(TaskRequest(**spec), ingress="http.tasks")
    _complete(queue, "impl-793")
    _complete(queue, "review-1", verdict="approve")
    _complete(queue, "test-793")
    _fail(queue, "fix-1")
    _assert_blocked_on(queue, "fix-1=failed")


def test_retry_fix_without_approve_rereview_still_vetoes(tmp_path):
    """#821 keeps the request_changes veto: a completed retry fix supersedes
    the timed-out round, but the vetoing review is unresolved until a later
    review approves it."""
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    queue.admit_work_intent(_intent(task=_task()))
    for spec in (_lineage_task("review-1", "review", "impl-793"),
                 _lineage_task("fix-1", "implement", "review-1", fix_round=1,
                               review_findings=["veto bug"]),
                 _lineage_task("test-793", "test", "review-1")):
        queue.enqueue(TaskRequest(**spec), ingress="http.tasks")
    _complete(queue, "impl-793")
    _complete(queue, "test-793")
    _complete(queue, "review-1", verdict="request_changes", findings=["veto bug"])
    _fail(queue, "fix-1", status="timed_out")
    _system_retry(queue, "fix-1-retry", "fix-1", "implement", fix_round=1,
                  review_findings=["veto bug"])
    _complete(queue, "fix-1-retry")
    assert queue.get_work_intent("directive-1")["state"] == "BOUND"
    queue.enqueue(TaskRequest(**_lineage_task("review-2", "review", "fix-1-retry")),
                  ingress="http.tasks")
    _complete(queue, "review-2", verdict="approve")
    assert queue.get_work_intent("directive-1")["state"] == "SUCCESS"


def test_failed_test_without_later_completed_test_does_not_succeed(tmp_path):
    """#821: a failed terminal test blocks until its system retry, of the
    same reviewed revision and the same dispatcher-persisted treatment,
    completed."""
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    queue.admit_work_intent(_intent(task=_task()))
    for spec in (_lineage_task("review-1", "review", "impl-793"),
                 _lineage_task("test-1", "test", "review-1", reviewed_sha="a" * 40)):
        queue.enqueue(TaskRequest(**spec), ingress="http.tasks")
    _dispatched_test(queue, "test-1")
    _complete(queue, "impl-793")
    _complete(queue, "review-1", verdict="approve")
    _fail(queue, "test-1")
    _assert_blocked_on(queue, "test-1=failed")
    _system_retry(queue, "test-1-retry", "test-1", "test", reviewed_sha="a" * 40)
    _dispatched_test(queue, "test-1-retry")
    _complete(queue, "test-1-retry")
    assert queue.get_work_intent("directive-1")["state"] == "SUCCESS"


def test_http_redispatched_fix_does_not_supersede_without_provenance(tmp_path):
    """#821: a fix round has no dispatcher-verified selection, so only trusted
    retry provenance supersedes it. A second fix off the same review, branch
    and findings via http.tasks -- every one of those claims caller-supplied
    -- does not."""
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    queue.admit_work_intent(_intent(task=_task()))
    for spec in (_lineage_task("review-1", "review", "impl-793"),
                 _lineage_task("fix-1", "implement", "review-1", fix_round=1,
                               review_findings=["veto bug"]),
                 _lineage_task("fix-again", "implement", "review-1", fix_round=1,
                               review_findings=["veto bug"]),
                 _lineage_task("test-793", "test", "review-1")):
        queue.enqueue(TaskRequest(**spec), ingress="http.tasks")
    _complete(queue, "impl-793")
    _complete(queue, "review-1", verdict="approve")
    _complete(queue, "test-793")
    _fail(queue, "fix-1", status="timed_out")
    _complete(queue, "fix-again")
    _assert_blocked_on(queue, "fix-1=timed_out")


def _setup_approved_lineage(tmp_path):
    db = tmp_path / "tasks.db"
    queue = TaskQueue(str(db))
    queue.admit_work_intent(_intent(task=_task()))
    queue.enqueue(TaskRequest(**_lineage_task("review-1", "review", "impl-793")),
                  ingress="http.tasks")
    _complete(queue, "impl-793")
    _complete(queue, "review-1", verdict="approve")
    return queue


def test_failed_full_suite_test_not_superseded_by_other_scope_test(tmp_path):
    """Review of PR #822 at 4afcda7, finding 1: a shared PR/branch is not proof
    of a retry. A later completed test the dispatcher sent out with a
    different treatment, and no retry provenance, does not stand in for the
    failed full-suite run, even off the same review."""
    queue = _setup_approved_lineage(tmp_path)
    queue.enqueue(TaskRequest(**_lineage_task("test-full", "test", "review-1")
                              | {"description": "Run the full suite"}),
                  ingress="http.tasks")
    _dispatched_test(queue, "test-full", scope="full_suite", digest="policy-h1")
    queue.submit_result("test-full", TaskResult(task_id="test-full", status="failed",
                                                summary="3 product tests failed"))
    queue.enqueue(TaskRequest(**_lineage_task("test-smoke", "test", "review-1")
                              | {"description": "Smoke-test the CLI"}),
                  ingress="http.tasks")
    _dispatched_test(queue, "test-smoke", scope="targeted", digest="policy-h2")
    _complete(queue, "test-smoke")
    _assert_blocked_on(queue, "test-full=failed")


def test_unscoped_tests_off_same_parent_do_not_supersede(tmp_path):
    """Review of PR #822 at e7f639a: two absent ``test_scope`` values are not
    a matching scope. Both tests arrive via http.tasks with caller-supplied
    ``prev_task_id`` and branch and no system scope claim, so nothing shows
    the smoke run is a retry of the failed full run."""
    queue = _setup_approved_lineage(tmp_path)
    queue.enqueue(TaskRequest(**_lineage_task("test-full", "test", "review-1",
                                              test_command="pytest tests")),
                  ingress="http.tasks")
    queue.submit_result("test-full", TaskResult(task_id="test-full", status="failed",
                                                summary="3 product tests failed"))
    queue.enqueue(TaskRequest(**_lineage_task("test-smoke", "test", "review-1",
                                              test_command="pytest tests/smoke")),
                  ingress="http.tasks")
    _complete(queue, "test-smoke")
    _assert_blocked_on(queue, "test-full=failed")


def test_same_reviewed_sha_tests_off_same_parent_do_not_supersede(tmp_path):
    """Review of PR #822 at 1624758: the reviewed commit is not test scope.
    Two different runs of one commit share its SHA; without a test-specific
    scope claim the smoke run does not stand in for the failed full run."""
    queue = _setup_approved_lineage(tmp_path)
    queue.enqueue(TaskRequest(**_lineage_task("test-full", "test", "review-1",
                                              reviewed_sha="a" * 40,
                                              test_command="pytest tests")),
                  ingress="http.tasks")
    queue.submit_result("test-full", TaskResult(task_id="test-full", status="failed",
                                                summary="3 product tests failed"))
    queue.enqueue(TaskRequest(**_lineage_task("test-smoke", "test", "review-1",
                                              reviewed_sha="a" * 40,
                                              test_command="pytest tests/smoke")),
                  ingress="http.tasks")
    _complete(queue, "test-smoke")
    _assert_blocked_on(queue, "test-full=failed")


def test_caller_supplied_test_scope_does_not_supersede(tmp_path):
    """Review of PR #822 at 97edc73: ``context.test_scope`` is a request claim
    the dispatcher logs and ignores (§7.2), so it proves nothing here either.
    Equal caller-supplied scopes on two different runs of one commit do not
    make the smoke run a retry of the failed full run."""
    queue = _setup_approved_lineage(tmp_path)
    queue.enqueue(TaskRequest(**_lineage_task("test-full", "test", "review-1",
                                              reviewed_sha="a" * 40,
                                              test_scope="targeted",
                                              test_command="pytest tests")),
                  ingress="http.tasks")
    queue.submit_result("test-full", TaskResult(task_id="test-full", status="failed",
                                                summary="3 product tests failed"))
    queue.enqueue(TaskRequest(**_lineage_task("test-smoke", "test", "review-1",
                                              reviewed_sha="a" * 40,
                                              test_scope="targeted",
                                              test_command="pytest tests/smoke")),
                  ingress="http.tasks")
    _complete(queue, "test-smoke")
    _assert_blocked_on(queue, "test-full=failed")


def test_declared_retry_of_other_revision_does_not_supersede(tmp_path):
    """A system retry that names the dead test but declares a different
    reviewed revision tested different code; it is not that test's retry."""
    queue = _setup_approved_lineage(tmp_path)
    queue.enqueue(TaskRequest(**_lineage_task("test-1", "test", "review-1",
                                              reviewed_sha="a" * 40)),
                  ingress="http.tasks")
    _fail(queue, "test-1", status="timed_out")
    queue.enqueue(TaskRequest(**_lineage_task("retry-test-1-a1", "test", "test-1",
                                              reviewed_sha="b" * 40,
                                              original_task_id="test-1",
                                              retry_of="test-1",
                                              retry_attempt=1)),
                  ingress="retry.failed_task",
                  _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
    _complete(queue, "retry-test-1-a1")
    _assert_blocked_on(queue, "test-1=timed_out")


def test_declared_retry_with_changed_selection_does_not_supersede(tmp_path):
    """Review of PR #822 at e7f639a: the declared-provenance path checks the
    test selection too. A system successor that names the dead test but was
    dispatched with a narrower selection is different work, not a retry."""
    queue = _setup_approved_lineage(tmp_path)
    queue.enqueue(TaskRequest(**_lineage_task("test-1", "test", "review-1",
                                              reviewed_sha="a" * 40)),
                  ingress="http.tasks")
    _dispatched_test(queue, "test-1", scope="full_suite", digest="policy-h1")
    _fail(queue, "test-1", status="timed_out")
    _system_retry(queue, "retry-test-1-a1", "test-1", "test", reviewed_sha="a" * 40)
    _dispatched_test(queue, "retry-test-1-a1", scope="targeted", digest="policy-h2")
    _complete(queue, "retry-test-1-a1")
    _assert_blocked_on(queue, "test-1=timed_out")


def test_same_policy_fingerprint_differing_run_does_not_supersede(tmp_path):
    """Review of PR #822 at 00c959a: the persisted ``test_scope_hash`` is a
    policy fingerprint, not an executed-test identity. Two different
    targeted runs under one policy -- same parent, branch, revision and
    hash, dispatched for real via the attribution rows -- are not shown to
    be the same run; only trusted retry provenance is."""
    queue = _setup_approved_lineage(tmp_path)
    queue.enqueue(TaskRequest(**_lineage_task("test-full", "test", "review-1",
                                              reviewed_sha="a" * 40,
                                              test_command="pytest tests")),
                  ingress="http.tasks")
    _dispatched_test(queue, "test-full", scope="targeted", digest="policy-h1")
    queue.submit_result("test-full", TaskResult(task_id="test-full", status="failed",
                                                summary="3 product tests failed"))
    queue.enqueue(TaskRequest(**_lineage_task("test-smoke", "test", "review-1",
                                              reviewed_sha="a" * 40,
                                              test_command="pytest tests/smoke")),
                  ingress="http.tasks")
    _dispatched_test(queue, "test-smoke", scope="targeted", digest="policy-h1")
    _complete(queue, "test-smoke")
    _assert_blocked_on(queue, "test-full=failed")


def test_declared_retry_provenance_supersedes_dead_task(tmp_path):
    """A system retry names its parent in ``original_task_id`` (what
    server._auto_retry_failed_task writes); that link supersedes the dead
    task even though the retry chains off it rather than off the review.
    The key survives admission only with the in-process provenance token;
    HTTP ingress drops it (``_trusted_enqueue_context``), so a caller cannot
    forge this path."""
    queue = _setup_approved_lineage(tmp_path)
    queue.enqueue(TaskRequest(**_lineage_task("test-1", "test", "review-1")),
                  ingress="http.tasks")
    _fail(queue, "test-1", status="timed_out")
    queue.enqueue(TaskRequest(**_lineage_task("retry-test-1-a1", "test", "test-1",
                                              original_task_id="test-1",
                                              retry_of="test-1",
                                              retry_attempt=1)),
                  ingress="retry.failed_task",
                  _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
    assert queue.get_work_intent("directive-1")["state"] == "BOUND"
    _complete(queue, "retry-test-1-a1")
    terminal = queue.get_work_intent("directive-1")
    assert terminal["state"] == "SUCCESS"
    assert "test-1" in terminal["decision_reason"]


def test_needs_human_task_is_never_superseded(tmp_path):
    """Review of PR #822 at 4afcda7, finding 2: a human-waiting task is not a
    dead round. Neither a same-step redispatch nor a declared retry resolves
    it; only a human can."""
    queue = _setup_approved_lineage(tmp_path)
    queue.enqueue(TaskRequest(**_lineage_task("test-held", "test", "review-1")),
                  ingress="http.tasks")
    queue.submit_result("test-held", TaskResult(task_id="test-held", status="needs_human",
                                                summary="manual approval required"))
    queue.enqueue(TaskRequest(**_lineage_task("test-2", "test", "review-1")),
                  ingress="http.tasks")
    _complete(queue, "test-2")
    _assert_blocked_on(queue, "test-held=needs_human")
    queue.enqueue(TaskRequest(**_lineage_task("retry-test-held-a1", "test", "test-held",
                                              original_task_id="test-held",
                                              retry_of="test-held",
                                              retry_attempt=1)),
                  ingress="retry.failed_task",
                  _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
    _complete(queue, "retry-test-held-a1")
    _assert_blocked_on(queue, "test-held=needs_human")


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
