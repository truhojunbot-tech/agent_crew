"""A suppressed review must hand its lineage to the survivor."""

import pytest
from fastapi.testclient import TestClient

from agent_crew import pipeline, server
from agent_crew.cea.cascade_contract import CascadeContract
from agent_crew.pipeline import auto_enqueue_fix, auto_enqueue_test
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


HEAD = "a" * 40
BRANCH = "agent/fix-706"
FINDING = "HIGH src/agent_crew/queue.py:3453 - missing review successor"


@pytest.fixture
def queue(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    return TaskQueue(tmp_db)


def _paired_reviews(queue, *, survivor_branch="main", survivor_pipeline=False,
                    block=True, survivor_round=None, blocked_round=None):
    shared = {"prev_task_id": "impl-706", "pr_number": 706}
    survivor_context = {**shared, "coordinator_managed": not survivor_pipeline,
                        "risk_tier": 1}
    if survivor_round is not None:
        survivor_context["fix_round"] = survivor_round
    queue.enqueue(TaskRequest(
        task_id="review-survivor", task_type="review", description="review PR",
        branch=survivor_branch, project="agent_crew", context=survivor_context))
    survivor = queue.dequeue(role="reviewer")
    assert survivor and survivor.task_id == "review-survivor"
    blocked_context = {**shared, "reviewed_sha": HEAD, "risk_tier": 2,
                       "issue": 706, "repo": "owner/repo", "implementer_agent": "codex"}
    if blocked_round is not None:
        blocked_context["fix_round"] = blocked_round
    queue.enqueue(TaskRequest(
        task_id="review-impl-706-r0", task_type="review", description="pipeline review",
        branch=BRANCH, project="agent_crew",
        context=blocked_context))
    assert queue.record_prepared_review_base("review-survivor", {"reviewed_sha": HEAD})
    blocked = queue.dequeue(role="reviewer")
    assert blocked and blocked.task_id == "review-impl-706-r0"
    if block:
        assert not queue.record_prepared_review_base(blocked.task_id, {"reviewed_sha": HEAD})
        assert queue.get_task_status(blocked.task_id) == "blocked"
    return blocked


def _result(queue, verdict):
    queue.submit_result("review-survivor", TaskResult(
        task_id="review-survivor", status="completed", verdict=verdict,
        summary="reviewed", findings=[FINDING] if verdict == "request_changes" else [],
        pr_number=706))


@pytest.mark.parametrize("verdict,successor_type", [
    ("request_changes", "implement"), ("approve", "test")])
def test_pending_survivor_adopts_enqueue_refusal_lineage(
        queue, verdict, successor_type):
    queue.enqueue(TaskRequest(
        task_id="impl-706", task_type="implement", description="implement",
        branch="main", project="agent_crew",
        context={"risk_tier": 2, "fix_round": 1, "issue": 706,
                 "repo": "owner/repo"}))
    queue.enqueue(TaskRequest(
        task_id="review-survivor", task_type="review", description="review PR",
        branch="main", project="agent_crew",
        context={"prev_task_id": "impl-706", "pr_number": 706,
                 "coordinator_managed": True, "risk_tier": 1}))
    result = TaskResult(task_id="impl-706", status="completed", summary="done",
                        branch=BRANCH, commit=HEAD, pr_number=706)
    assert pipeline.auto_enqueue_review(
        queue, "impl-706", pr_number=706, result=result,
        pr_state_fn=lambda _: "open") == "review-survivor"
    assert queue.get_task_status("review-impl-706-r1") is None
    survivor = queue.get_task("review-survivor")
    assert survivor.status == "pending"
    assert survivor.context["cascade_branch"] == BRANCH
    assert survivor.context["duplicate_review_lineage_from"] == "review-impl-706-r1"
    assert survivor.context["fix_round"] == 1
    assert survivor.context["risk_tier"] == 2
    assert survivor.context["issue"] == 706
    assert queue.dequeue(role="reviewer").task_id == "review-survivor"
    assert queue.record_prepared_review_base("review-survivor", {"reviewed_sha": HEAD})
    _result(queue, verdict)

    kwargs = {"pr_state_fn": lambda _: "open"}
    if verdict == "request_changes":
        successor = auto_enqueue_fix(queue, "review-survivor",
                                     head_sha_fn=lambda _: HEAD, **kwargs)
        assert auto_enqueue_fix(queue, "review-survivor",
                                head_sha_fn=lambda _: HEAD, **kwargs) is None
    else:
        successor = auto_enqueue_test(queue, "review-survivor", **kwargs)
        assert auto_enqueue_test(queue, "review-survivor", **kwargs) == successor
    assert successor is not None
    task = queue.get_task(successor)
    assert task.task_type == successor_type
    assert task.branch == BRANCH
    assert task.context["prev_task_id"] == "review-survivor"
    assert task.context["risk_tier"] == 2
    assert task.context["fix_round"] == (2 if verdict == "request_changes" else 1)
    assert len([item for item in queue.list_tasks() if item.task_id == successor]) == 1


def test_request_changes_uses_pipeline_branch_lineage_and_tier_once(queue):
    _paired_reviews(queue)
    survivor = queue.get_task("review-survivor")
    assert survivor.branch == "main"
    assert survivor.context["cascade_branch"] == BRANCH
    assert survivor.context["issue"] == 706
    assert survivor.context["repo"] == "owner/repo"
    assert survivor.context["risk_tier"] == 2
    _result(queue, "request_changes")

    kwargs = {"pr_state_fn": lambda _: "open", "head_sha_fn": lambda _: HEAD}
    fix_id = auto_enqueue_fix(queue, survivor.task_id, **kwargs)
    assert fix_id is not None
    assert auto_enqueue_fix(queue, survivor.task_id, **kwargs) is None
    fix = queue.get_task(fix_id)
    assert fix.branch == BRANCH
    assert fix.context["prev_task_id"] == survivor.task_id
    assert fix.context["fix_round"] == 1
    assert fix.context["issue"] == 706
    assert fix.context["risk_tier"] == 2
    assert len([task for task in queue.list_tasks() if task.task_id == fix_id]) == 1


def test_approve_uses_normal_test_path_with_inherited_metadata(queue):
    _paired_reviews(queue)
    _result(queue, "approve")
    test_id = auto_enqueue_test(queue, "review-survivor",
                                pr_state_fn=lambda _: "open")
    assert test_id is not None
    test = queue.get_task(test_id)
    assert test.branch == BRANCH
    assert test.context["prev_task_id"] == "review-survivor"
    assert test.context["risk_tier"] == 2
    assert auto_enqueue_test(queue, "review-survivor",
                             pr_state_fn=lambda _: "open") == test_id
    assert len([task for task in queue.list_tasks() if task.task_id == test_id]) == 1


def test_blocked_review_tier3_gate_is_preserved(queue):
    _paired_reviews(queue)
    queue.patch_context("review-impl-706-r0", {"cea_cascade": CascadeContract(
        enforced=True, tier=3, human_gate_required=True).as_record()})
    _result(queue, "approve")
    assert auto_enqueue_test(queue, "review-survivor",
                             pr_state_fn=lambda _: "open") is None
    assert any(gate.id == "risk-tier3-test-review-survivor"
               for gate in queue.list_gates())
    assert not [task for task in queue.list_tasks() if task.task_type == "test"]


def test_two_pipeline_reviews_do_not_double_cascade(queue):
    _paired_reviews(queue, survivor_branch=BRANCH, survivor_pipeline=True)
    _result(queue, "request_changes")
    kwargs = {"pr_state_fn": lambda _: "open", "head_sha_fn": lambda _: HEAD}
    first = auto_enqueue_fix(queue, "review-survivor", **kwargs)
    assert first is not None
    assert auto_enqueue_fix(queue, "review-survivor", **kwargs) is None
    assert len([task for task in queue.list_tasks() if task.task_type == "implement"]) == 1


def test_survivor_keeps_later_fix_round_and_higher_tier(queue):
    _paired_reviews(queue, survivor_round=0, blocked_round=1)
    survivor = queue.get_task("review-survivor")
    assert survivor.context["fix_round"] == 1
    assert survivor.context["risk_tier"] == 2


def test_second_suppressed_review_cannot_replace_first_handoff(queue):
    shared = {"prev_task_id": "impl-706", "pr_number": 706}
    queue.enqueue(TaskRequest(task_id="review-survivor", task_type="review",
                              description="review", branch="main", project="agent_crew",
                              context=shared))
    assert queue.dequeue(role="reviewer").task_id == "review-survivor"
    for task_id, sha, round_ in (("pipeline-first", HEAD, 0),
                                 ("pipeline-second", None, 1)):
        context = {**shared, "fix_round": round_, "risk_tier": 1,
                   "repo": "owner/repo"}
        if sha:
            context["reviewed_sha"] = sha
        queue.enqueue(TaskRequest(task_id=task_id, task_type="review",
                                  description="review", branch=BRANCH,
                                  project="agent_crew", context=context))
    assert queue.record_prepared_review_base("review-survivor", {"reviewed_sha": HEAD})
    first = queue.dequeue(role="reviewer")
    assert first.task_id == "pipeline-first"
    assert not queue.record_prepared_review_base(first.task_id, {"reviewed_sha": HEAD})
    _result(queue, "request_changes")
    kwargs = {"pr_state_fn": lambda _: "open", "head_sha_fn": lambda _: HEAD}
    assert auto_enqueue_fix(queue, "review-survivor", **kwargs) is not None
    second = queue.dequeue(role="reviewer")
    assert second.task_id == "pipeline-second"
    assert not queue.record_prepared_review_base(second.task_id, {"reviewed_sha": HEAD})
    assert queue.get_task_context("review-survivor")["duplicate_review_lineage_from"] == first.task_id
    assert queue.get_task_context("review-survivor")["fix_round"] == 0
    assert auto_enqueue_fix(queue, "review-survivor", **kwargs) is None
    assert len([task for task in queue.list_tasks() if task.task_type == "implement"]) == 1


@pytest.mark.parametrize("verdict,successor_type", [
    ("request_changes", "implement"), ("approve", "test")])
def test_survivor_http_result_runs_one_existing_cascade(
        queue, monkeypatch, verdict, successor_type):
    _paired_reviews(queue)
    monkeypatch.setattr(pipeline, "pr_is_actionable", lambda *a, **k: (True, "open"))
    monkeypatch.setattr(pipeline, "review_head_status",
                        lambda *a, **k: ("current", HEAD, "same head"))
    monkeypatch.setattr(server, "review_publication_decision",
                        lambda *a, **k: pipeline.ReviewPublication(True, "current", "same head"))
    from agent_crew import github
    monkeypatch.setattr(github, "post_review_comment", lambda **k: True)
    app = server.create_app(queue._db_path, project="agent_crew", pane_map={},
                            worktree_map={}, watchdog_disabled=True,
                            anomaly_disabled=True)
    payload = {"task_id": "review-survivor", "status": "completed",
               "summary": "Reviewed the current PR head and checked all requested changes.",
               "verdict": verdict,
               "findings": [FINDING] if verdict == "request_changes" else [],
               "pr_number": 706}
    headers = {"X-Agent-Crew-Project": "agent_crew"}
    with TestClient(app) as client:
        first = client.post("/tasks/review-survivor/result", json=payload, headers=headers)
        assert first.status_code == 200, first.text
        second = client.post("/tasks/review-survivor/result", json=payload, headers=headers)
        assert second.status_code == 200, second.text

    successors = [task for task in queue.list_tasks() if task.task_type == successor_type]
    assert len(successors) == 1
    assert successors[0].branch == BRANCH
    assert successors[0].context["prev_task_id"] == "review-survivor"


def test_completed_survivor_is_cascaded_when_duplicate_is_blocked(
        queue, monkeypatch):
    blocked = _paired_reviews(queue, block=False)
    _result(queue, "request_changes")
    monkeypatch.setattr(pipeline, "pr_is_actionable", lambda *a, **k: (True, "open"))
    monkeypatch.setattr(pipeline, "review_head_status",
                        lambda *a, **k: ("current", HEAD, "same head"))
    monkeypatch.setattr(server, "review_publication_decision",
                        lambda *a, **k: pipeline.ReviewPublication(True, "current", "same head"))
    app = server.create_app(queue._db_path, project="agent_crew", pane_map={},
                            worktree_map={}, watchdog_disabled=True,
                            anomaly_disabled=True)
    with TestClient(app):
        assert not app.state.record_prepared_base(blocked, "reviewer", HEAD, "test")
        assert not app.state.record_prepared_base(blocked, "reviewer", HEAD, "test")
    fixes = [task for task in queue.list_tasks() if task.task_type == "implement"]
    assert len(fixes) == 1
    assert fixes[0].branch == BRANCH
