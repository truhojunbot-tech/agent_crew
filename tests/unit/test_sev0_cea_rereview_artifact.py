"""A new PR head is new review/test work; replaying one head remains P4 work."""
import sqlite3

import pytest

from agent_crew.cea import store
from agent_crew.cea.engine import intent_hash
from agent_crew.pipeline import auto_enqueue_test
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import intent_for_task
from tests.unit.sev0_cea_acceptance_helpers import AuthorityState, enqueue_and_read, queue_for


SHA_A = "a" * 40
SHA_B = "b" * 40


def _task(task_id, task_type, sha):
    return TaskRequest(task_id=task_id, task_type=task_type,
                       description="Review PR #51", branch="main", priority=3,
                       project="agent_crew", context={
                           "repo": "example/agent_crew", "pr_number": 51,
                           "reviewed_sha": sha})


@pytest.mark.parametrize("task_type", ["review", "test"])
def test_new_head_changes_review_or_test_intent_but_same_head_does_not(task_type):
    first = intent_for_task(_task("first", task_type, SHA_A)).identity
    replay = intent_for_task(_task("replay", task_type, SHA_A)).identity
    changed = intent_for_task(_task("changed", task_type, SHA_B)).identity
    assert intent_hash(first) == intent_hash(replay)
    assert intent_hash(first) != intent_hash(changed)


@pytest.mark.parametrize("task_type", ["review", "test"])
@pytest.mark.parametrize("mode", ["test", "shadow"])
def test_completed_head_blocks_replay_but_allows_new_head(tmp_path, task_type, mode):
    q = queue_for(tmp_path, AuthorityState("active"), name="heads.db", mode=mode)
    admitted, first = enqueue_and_read(q, _task("first", task_type, SHA_A), ingress="cli.enqueue")
    assert admitted and first["decision"] == "ALLOW"
    with sqlite3.connect(q._db_path) as conn:
        store.set_lineage_state(conn, first["intent_hash"], first["receipt_id"], "CONSUMED")

    replay_task = _task("replay", task_type, SHA_A)
    replay_task.context["allow_duplicate_review"] = True
    replay_admitted, replay = enqueue_and_read(
        q, replay_task, ingress="cli.enqueue")
    assert replay_admitted is (mode == "shadow")
    assert replay["decision"] == "BLOCK"
    assert replay["reason"]["code"] == "ALREADY_COMPLETED"

    changed_admitted, changed = enqueue_and_read(
        q, _task("changed", task_type, SHA_B), ingress="cli.enqueue")
    assert changed_admitted
    assert changed["decision"] == "ALLOW"
    assert changed["intent_hash"] != first["intent_hash"]


def test_implement_identity_ignores_reviewed_sha():
    first = intent_for_task(_task("first", "implement", SHA_A)).identity
    changed = intent_for_task(_task("changed", "implement", SHA_B)).identity
    assert intent_hash(first) == intent_hash(changed)


def test_unpinned_fix_round_distinguishes_rereview():
    first = _task("first", "review", "")
    changed = _task("changed", "review", "")
    first.context["fix_round"] = 1
    changed.context["fix_round"] = 2
    assert intent_hash(intent_for_task(first).identity) != intent_hash(intent_for_task(changed).identity)


def test_reviewed_sha_takes_precedence_over_round_and_head_hint():
    first = _task("first", "review", SHA_A)
    replay = _task("replay", "review", SHA_A)
    first.context.update(fix_round=1, expected_head_sha=SHA_B)
    replay.context.update(fix_round=2, expected_head_sha="c" * 40)
    assert intent_hash(intent_for_task(first).identity) == intent_hash(intent_for_task(replay).identity)


def test_review_cascade_passes_artifact_to_test(tmp_path):
    q = queue_for(tmp_path, AuthorityState("active"), name="cascade.db", mode="shadow")
    review = _task("review-one", "review", SHA_A)
    review.context["fix_round"] = 1
    q.enqueue(review, ingress="cli.enqueue")
    assert q.dequeue(role="reviewer") is not None
    q.submit_result(review.task_id, TaskResult(
        task_id=review.task_id, status="completed", summary="approved", verdict="approve"))
    test_id = auto_enqueue_test(q, review.task_id, pr_state_fn=lambda *args, **kwargs: "open")
    assert test_id is not None
    test_task = next(t for t in q.list_tasks() if t.task_id == test_id)
    assert test_task.context["reviewed_sha"] == SHA_A
    assert test_task.context["fix_round"] == 1
    assert f"commit:{SHA_A}" in intent_for_task(test_task).identity.target.scope_anchors
