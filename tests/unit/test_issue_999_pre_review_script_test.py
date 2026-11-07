"""#999: the script tester's suite runs BEFORE the review is dispatched.

8 of 14 request_changes in a week were tests outside the touched files, found
only after a review had been spent. With the script tester, an implement (or
fix) result with a PR now goes to a pre-review test first:

  * new failures vs base -> one implementer fix round, zero review tasks;
  * green -> exactly one review, as before;
  * the suite could not judge the head -> the review runs anyway, with the
    reason recorded.

No real suite runs here: the script test result is posted the way
``_run_script_test_task`` posts it, or produced by a fake runner.
"""

import json

import pytest
from fastapi.testclient import TestClient

from agent_crew import script_tester
from agent_crew.pipeline import (
    PRE_REVIEW_OUTCOME_KEY,
    fix_task_id,
    pre_review_test_id,
)
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app

PR = 998
HEAD = "c" * 40
BRANCH = "agent/999-pre-review"
FAILING = "tests/unit/test_other.py::test_outside_touched_files"


def _harness(tmp_path, monkeypatch, *, role_agents=None, impl_context=None):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DELIVERY", "off")
    import agent_crew.github as github
    monkeypatch.setattr(github, "pr_state", lambda *a, **k: "open")
    monkeypatch.setattr(github, "pr_head_sha", lambda *a, **k: HEAD)
    monkeypatch.setattr(github, "pr_number_for_branch", lambda *a, **k: PR)
    monkeypatch.setattr(github, "post_review_comment", lambda *a, **k: True)
    monkeypatch.setattr("agent_crew.pipeline._repo_default_branch", lambda repo: "main")
    monkeypatch.setattr("agent_crew.server.verify_implement_artifact",
                        lambda _task, _result, *, repo_cwd: (True, "verified artifact"))
    state = {"project": "agent_crew"}
    if role_agents:
        state["role_agents"] = role_agents
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state))
    db = str(tmp_path / "tasks.db")
    notices = []
    app = create_app(db, project="agent_crew", pane_map={}, identity_required=False,
                     watchdog_disabled=True, anomaly_disabled=True,
                     state_path=str(state_path),
                     coordinator_notify_fn=lambda message: notices.append(message) or True)
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest(
        task_id="impl-999", task_type="implement", description="Implement #999",
        branch=BRANCH, project="agent_crew",
        context={"issue": 999, "risk_tier": 2, "repo": "owner/repo",
                 "base_branch": "main", **(impl_context or {})}),
        ingress="http.tasks")
    return app, queue, notices


def _complete_impl(client, task_id="impl-999"):
    response = client.post(f"/tasks/{task_id}/result", json={
        "task_id": task_id, "status": "completed", "summary": "implemented",
        "branch": BRANCH, "commit": HEAD, "pr_number": PR})
    assert response.status_code == 200, response.text


def _post_script(client, task_id, record):
    result = script_tester.record_to_result(task_id, record, pr_number=PR)
    response = client.post(f"/tasks/{task_id}/result", json={
        "task_id": task_id, "status": result.status, "summary": result.summary,
        "findings": result.findings, "pr_number": PR, "error_info": result.error_info})
    assert response.status_code == 200, response.text


RED = {"executor": "script", "outcome": script_tester.FAILED, "reason": "new_failures",
       "new_failures": [FAILING], "counts": {"passed": 10, "failed": 1}}
GREEN = {"executor": "script", "outcome": script_tester.PASSED, "counts": {"passed": 11}}


def _of_type(queue, task_type):
    return [t for t in queue.list_tasks() if t.task_type == task_type]


def test_implement_result_goes_to_pre_review_test_not_review(tmp_path, monkeypatch):
    app, queue, _ = _harness(tmp_path, monkeypatch)
    with TestClient(app) as client:
        _complete_impl(client)
    tests = _of_type(queue, "test")
    assert [t.task_id for t in tests] == [pre_review_test_id("impl-999")]
    ctx = tests[0].context
    assert ctx["pre_review"] is True and ctx["prev_task_id"] == "impl-999"
    assert ctx["pr_number"] == PR and ctx["reviewed_sha"] == HEAD
    assert ctx["base_branch"] == "main" and ctx["repo"] == "owner/repo"
    assert _of_type(queue, "review") == []


def test_ac1_new_failures_make_one_fix_round_and_zero_reviews(tmp_path, monkeypatch):
    app, queue, notices = _harness(tmp_path, monkeypatch)
    pretest = pre_review_test_id("impl-999")
    with TestClient(app) as client:
        _complete_impl(client)
        _post_script(client, pretest, RED)
        _post_script(client, pretest, RED)        # a replayed result adds nothing
    fixes = [t for t in _of_type(queue, "implement") if t.task_id != "impl-999"]
    assert [t.task_id for t in fixes] == [fix_task_id(pretest, 1)]
    fix = fixes[0]
    assert fix.context["review_findings"] == [f"new failure: {FAILING}"]
    assert FAILING in fix.description and "no review spent" in fix.description
    assert fix.context["prev_task_id"] == pretest and fix.branch == BRANCH
    assert fix.context["pr_number"] == PR
    assert _of_type(queue, "review") == []
    assert notices == []


def test_ac2_green_suite_enqueues_exactly_one_review(tmp_path, monkeypatch):
    app, queue, _ = _harness(tmp_path, monkeypatch)
    pretest = pre_review_test_id("impl-999")
    merges = []
    monkeypatch.setattr("agent_crew.github.merge_pr", lambda *a, **k: merges.append(a))
    with TestClient(app) as client:
        _complete_impl(client)
        _post_script(client, pretest, GREEN)
        _post_script(client, pretest, GREEN)
    reviews = _of_type(queue, "review")
    assert [r.task_id for r in reviews] == ["review-impl-999-r0"]
    assert reviews[0].context["prev_task_id"] == "impl-999"
    assert reviews[0].context["reviewed_sha"] == HEAD
    assert reviews[0].context[PRE_REVIEW_OUTCOME_KEY] == {
        "task_id": pretest, "outcome": "passed"}
    assert queue.get_task("impl-999").context[PRE_REVIEW_OUTCOME_KEY]["outcome"] == "passed"
    assert merges == []                        # a pre-review pass is not a merge gate
    assert [t.task_id for t in _of_type(queue, "test")] == [pretest]


def test_ac3_flag_off_restores_review_first(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_PRE_REVIEW_SCRIPT_TEST", "0")
    app, queue, _ = _harness(tmp_path, monkeypatch)
    with TestClient(app) as client:
        _complete_impl(client)
    assert [r.task_id for r in _of_type(queue, "review")] == ["review-impl-999-r0"]
    assert _of_type(queue, "test") == []
    assert PRE_REVIEW_OUTCOME_KEY not in queue.get_task("review-impl-999-r0").context


def test_ac4_provider_tester_keeps_review_first(tmp_path, monkeypatch):
    app, queue, _ = _harness(tmp_path, monkeypatch, role_agents={
        "implementer": "codex", "reviewer": "claude", "tester": "gemini"})
    with TestClient(app) as client:
        _complete_impl(client)
    assert [r.task_id for r in _of_type(queue, "review")] == ["review-impl-999-r0"]
    assert _of_type(queue, "test") == []


def test_ac5_pre_review_fix_round_does_not_spend_a_review_round(tmp_path, monkeypatch):
    # One review-fix round is all the budget allows. A pre-review round that
    # counted against it would leave no room for the review's own fix.
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "1")
    app, queue, _ = _harness(tmp_path, monkeypatch)
    pretest = pre_review_test_id("impl-999")
    fix_id = fix_task_id(pretest, 1)
    with TestClient(app) as client:
        _complete_impl(client)
        _post_script(client, pretest, RED)
        fix = queue.get_task(fix_id)
        assert fix.context["fix_round"] == 0
        assert fix.context["pre_review_fix_round"] == 1
        _complete_impl(client, fix_id)
        # The fix's own head is screened again before its review.
        second = pre_review_test_id(fix_id)
        assert queue.get_task(second).context["pre_review_fix_round"] == 1
        assert _of_type(queue, "review") == []
        _post_script(client, second, GREEN)
        review_id = f"review-{fix_id}-r0"
        assert [r.task_id for r in _of_type(queue, "review")] == [review_id]
        response = client.post(f"/tasks/{review_id}/result", json={
            "task_id": review_id, "status": "completed", "verdict": "request_changes",
            "summary": "request_changes: the new branch is missing its error path",
            "findings": ["code_quality: src/x.py:1 - missing error path"],
            "pr_number": PR})
        assert response.status_code == 200, response.text
    review_fix = queue.get_task(fix_task_id(review_id, 1))
    assert review_fix is not None, "the review's fix found its budget already spent"
    assert review_fix.context["fix_round"] == 1


def test_pre_review_fix_rounds_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "1")
    app, queue, _ = _harness(tmp_path, monkeypatch)
    pretest = pre_review_test_id("impl-999")
    fix_id = fix_task_id(pretest, 1)
    with TestClient(app) as client:
        _complete_impl(client)
        _post_script(client, pretest, RED)
        _complete_impl(client, fix_id)
        _post_script(client, pre_review_test_id(fix_id), RED)
    assert queue.get_task(fix_task_id(pre_review_test_id(fix_id), 2)) is None
    assert _of_type(queue, "review") == []


@pytest.mark.parametrize("record, reason", [
    ({"executor": "script", "outcome": script_tester.SKIPPED_NO_TEST_COMMAND},
     script_tester.SKIPPED_NO_TEST_COMMAND),
    ({"executor": "script", "outcome": script_tester.SETUP_ERROR, "detail": "no checkout"},
     script_tester.SETUP_ERROR),
    ({"executor": "script", "outcome": script_tester.INCOMPLETE,
      "reason": "script_test_timeout"}, "script_test_timeout"),
    ({"executor": "script", "outcome": script_tester.FAILED, "reason": "exit_code_2",
      "counts": {}}, "script_test_exit_code_2"),
])
def test_ac6_infra_error_falls_back_to_review_with_reason(
        tmp_path, monkeypatch, record, reason):
    app, queue, _ = _harness(tmp_path, monkeypatch)
    pretest = pre_review_test_id("impl-999")
    with TestClient(app) as client:
        _complete_impl(client)
        _post_script(client, pretest, record)
    reviews = _of_type(queue, "review")
    assert [r.task_id for r in reviews] == ["review-impl-999-r0"]
    expected = {"task_id": pretest, "outcome": "not_run", "reason": reason}
    assert reviews[0].context[PRE_REVIEW_OUTCOME_KEY] == expected
    assert queue.get_task("impl-999").context[PRE_REVIEW_OUTCOME_KEY] == expected
    assert queue.get_task(pretest).context[PRE_REVIEW_OUTCOME_KEY] == expected
    assert [t for t in _of_type(queue, "implement") if t.task_id != "impl-999"] == []


def test_ac6_runner_exception_falls_back_to_review(tmp_path, monkeypatch):
    app, queue, _ = _harness(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("runner exploded")

    monkeypatch.setattr(script_tester, "execute_test_task", boom)
    with TestClient(app) as client:
        _complete_impl(client)
        app.state.drain_script_tests()
    reviews = _of_type(queue, "review")
    assert [r.task_id for r in reviews] == ["review-impl-999-r0"]
    assert reviews[0].context[PRE_REVIEW_OUTCOME_KEY]["reason"] == "script_test_exception"


def test_pre_review_fake_runner_red_suite_end_to_end(tmp_path, monkeypatch):
    """The real ``_run_script_test_task`` path with a fake runner result."""
    app, queue, _ = _harness(tmp_path, monkeypatch)
    seen = []

    def fake_execute(task, *, repo, state, cache_dir, runner=None):
        seen.append(task.context.get("pre_review"))
        return script_tester.record_to_result(task.task_id, RED, pr_number=PR)

    monkeypatch.setattr(script_tester, "execute_test_task", fake_execute)
    with TestClient(app) as client:
        _complete_impl(client)
        app.state.drain_script_tests()
    assert seen == [True]
    pretest = pre_review_test_id("impl-999")
    assert queue.get_task(fix_task_id(pretest, 1)) is not None
    assert _of_type(queue, "review") == []


def test_no_pr_keeps_review_first(tmp_path, monkeypatch):
    """A crew-run branch whose PR the CLI opens later (#666) has no head to screen."""
    app, queue, _ = _harness(tmp_path, monkeypatch, impl_context={"crew_run_branch": True})
    with TestClient(app) as client:
        response = client.post("/tasks/impl-999/result", json={
            "task_id": "impl-999", "status": "completed", "summary": "implemented",
            "branch": BRANCH, "commit": HEAD})
        assert response.status_code == 200, response.text
    assert _of_type(queue, "test") == []
    assert len(_of_type(queue, "review")) == 1


def test_completed_pre_review_test_does_not_block_post_review_test(tmp_path, monkeypatch):
    """Same PR, same head: the post-review test is a different stage (#999)."""
    from agent_crew.pipeline import auto_enqueue_test
    app, queue, _ = _harness(tmp_path, monkeypatch)
    pretest = pre_review_test_id("impl-999")
    with TestClient(app) as client:
        _complete_impl(client)
        _post_script(client, pretest, GREEN)
    review_id = "review-impl-999-r0"
    from agent_crew.protocol import TaskResult
    queue.submit_result(review_id, TaskResult(
        task_id=review_id, status="completed", verdict="approve", findings=[],
        summary="approve: the change is correct and fully covered by tests",
        pr_number=PR))
    test_id = auto_enqueue_test(queue, review_id, pr_state_fn=lambda *a, **k: "open",
                                repo="owner/repo")
    assert test_id == f"test-{review_id}"
    post = queue.get_task(test_id)
    assert post is not None and not post.context.get("pre_review")


def test_flag_is_on_by_default(monkeypatch):
    from agent_crew.pipeline import pre_review_script_test_enabled
    monkeypatch.delenv("AGENT_CREW_PRE_REVIEW_SCRIPT_TEST", raising=False)
    assert pre_review_script_test_enabled() is True
    monkeypatch.setenv("AGENT_CREW_PRE_REVIEW_SCRIPT_TEST", "0")
    assert pre_review_script_test_enabled() is False


def test_cli_driven_loop_keeps_review_first(tmp_path, monkeypatch):
    """`crew run` / `discuss --then-run` wait for the review successor themselves."""
    from agent_crew import loop
    assert loop._TDD_CONTEXT["cli_driven_loop"] is True
    app, queue, _ = _harness(tmp_path, monkeypatch, impl_context={"cli_driven_loop": True})
    with TestClient(app) as client:
        _complete_impl(client)
    assert [r.task_id for r in _of_type(queue, "review")] == ["review-impl-999-r0"]
    assert _of_type(queue, "test") == []


# ── review of PR #1003: every implement-result route, not just HTTP ────────

def _mcp_harness(tmp_path, monkeypatch, *, role_agents=None, flag=None):
    """The MCP transport with no server: role map from state.json beside the DB."""
    from agent_crew.mcp_server import build_mcp_server
    if flag is not None:
        monkeypatch.setenv("AGENT_CREW_PRE_REVIEW_SCRIPT_TEST", flag)
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.delenv("AGENT_CREW_STATE", raising=False)
    import agent_crew.github as github
    monkeypatch.setattr(github, "pr_state", lambda *a, **k: "open")
    monkeypatch.setattr(github, "pr_head_sha", lambda *a, **k: HEAD)
    monkeypatch.setattr(github, "pr_number_for_branch", lambda *a, **k: PR)
    monkeypatch.setattr("agent_crew.pipeline._repo_default_branch", lambda repo: "main")
    monkeypatch.setattr("agent_crew.mcp_server.verify_implement_artifact",
                        lambda _task, _result, *, repo_cwd: (True, "verified artifact"))
    state = {"project": "agent_crew"}
    if role_agents:
        state["role_agents"] = role_agents
    (tmp_path / "state.json").write_text(json.dumps(state))
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest(
        task_id="impl-999", task_type="implement", description="Implement #999",
        branch=BRANCH, project="agent_crew",
        context={"issue": 999, "risk_tier": 2, "repo": "owner/repo",
                 "base_branch": "main"}), ingress="http.tasks")
    submit = build_mcp_server(db)._tool_manager._tools["submit_result"].fn
    return queue, submit


def _mcp_impl(submit, task_id="impl-999"):
    response = submit(task_id=task_id, status="completed", summary="implemented",
                      branch=BRANCH, commit=HEAD, pr_number=PR)
    assert response["acknowledged"] is True, response


def _mcp_script(submit, task_id, record):
    result = script_tester.record_to_result(task_id, record, pr_number=PR)
    response = submit(task_id=task_id, status=result.status, summary=result.summary,
                      findings=result.findings, pr_number=PR, error_info=result.error_info)
    assert response["acknowledged"] is True, response


def test_mcp_implement_result_goes_to_pre_review_test(tmp_path, monkeypatch):
    queue, submit = _mcp_harness(tmp_path, monkeypatch)
    _mcp_impl(submit)
    assert [t.task_id for t in _of_type(queue, "test")] == [pre_review_test_id("impl-999")]
    assert _of_type(queue, "review") == []


def test_mcp_green_pre_review_enqueues_exactly_one_review(tmp_path, monkeypatch):
    queue, submit = _mcp_harness(tmp_path, monkeypatch)
    pretest = pre_review_test_id("impl-999")
    _mcp_impl(submit)
    _mcp_script(submit, pretest, GREEN)
    reviews = _of_type(queue, "review")
    assert [r.task_id for r in reviews] == ["review-impl-999-r0"]
    assert reviews[0].context[PRE_REVIEW_OUTCOME_KEY] == {"task_id": pretest,
                                                          "outcome": "passed"}
    assert [t.task_id for t in _of_type(queue, "test")] == [pretest]   # no post-review test yet


def test_mcp_red_pre_review_enqueues_one_fix_and_zero_reviews(tmp_path, monkeypatch):
    queue, submit = _mcp_harness(tmp_path, monkeypatch)
    pretest = pre_review_test_id("impl-999")
    _mcp_impl(submit)
    _mcp_script(submit, pretest, RED)
    fixes = [t for t in _of_type(queue, "implement") if t.task_id != "impl-999"]
    assert [t.task_id for t in fixes] == [fix_task_id(pretest, 1)]
    assert fixes[0].context["review_findings"] == [f"new failure: {FAILING}"]
    assert fixes[0].context["fix_round"] == 0
    assert _of_type(queue, "review") == []


def test_mcp_pre_review_infra_error_reviews_with_reason(tmp_path, monkeypatch):
    queue, submit = _mcp_harness(tmp_path, monkeypatch)
    _mcp_impl(submit)
    _mcp_script(submit, pre_review_test_id("impl-999"),
                {"executor": "script", "outcome": script_tester.INCOMPLETE,
                 "reason": "script_test_timeout"})
    reviews = _of_type(queue, "review")
    assert len(reviews) == 1
    assert reviews[0].context[PRE_REVIEW_OUTCOME_KEY]["reason"] == "script_test_timeout"
    assert [t for t in _of_type(queue, "implement") if t.task_id != "impl-999"] == []


@pytest.mark.parametrize("flag, role_agents", [
    ("0", None),
    (None, {"implementer": "codex", "reviewer": "claude", "tester": "gemini"}),
])
def test_mcp_flag_off_or_provider_tester_keeps_review_first(
        tmp_path, monkeypatch, flag, role_agents):
    queue, submit = _mcp_harness(tmp_path, monkeypatch, role_agents=role_agents, flag=flag)
    _mcp_impl(submit)
    assert [r.task_id for r in _of_type(queue, "review")] == ["review-impl-999-r0"]
    assert _of_type(queue, "test") == []


def _cloud_task(queue):
    queue.enqueue(TaskRequest(
        task_id="impl-cloud-999", task_type="implement", description="Implement #999",
        branch=BRANCH, project="agent_crew",
        context={"issue": 999, "risk_tier": 2, "repo": "owner/repo",
                 "base_branch": "main"}), ingress="http.tasks")
    return queue.get_task("impl-cloud-999")


def _reconcile_cloud(queue, task):
    from agent_crew import claude_cloud
    return claude_cloud.reconcile_cloud_dispatch(
        queue, task, repo="owner/repo",
        pr_number_for_branch_fn=lambda *a, **k: PR,
        pr_head_sha_fn=lambda *a, **k: HEAD,
        commit_message_fn=lambda *a, **k: None,
        pr_state_fn=lambda *a, **k: "open")


@pytest.mark.parametrize("flag, expect_pretest", [(None, True), ("0", False)])
def test_cloud_pr_discovery_follows_the_same_policy(tmp_path, monkeypatch, flag, expect_pretest):
    queue, _ = _mcp_harness(tmp_path, monkeypatch, flag=flag)
    task = _cloud_task(queue)
    outcome = _reconcile_cloud(queue, task)
    assert outcome.action == "pr_ready"
    assert queue.get_task("impl-cloud-999").status == "completed"
    tests = [t.task_id for t in _of_type(queue, "test")]
    reviews = [r.task_id for r in _of_type(queue, "review")]
    if expect_pretest:
        assert tests == [pre_review_test_id("impl-cloud-999")] and reviews == []
    else:
        assert tests == [] and reviews == ["review-impl-cloud-999-r0"]
    # A second reconcile pass creates nothing new.
    _reconcile_cloud(queue, queue.get_task("impl-cloud-999"))
    assert [t.task_id for t in _of_type(queue, "test")] == tests
    assert [r.task_id for r in _of_type(queue, "review")] == reviews


def test_every_implement_result_route_asks_the_shared_policy():
    """Static guard: every caller of auto_enqueue_review names its #999 policy,
    so a new implement-result route cannot silently default to review-first."""
    import ast
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "agent_crew"
    missing = []
    for path in sorted(src.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "id", getattr(node.func, "attr", "")) in (
                        "auto_enqueue_review", "_pipeline_auto_enqueue_review")
                    and not any(kw.arg == "pre_review_test" for kw in node.keywords)):
                missing.append(f"{path.name}:{node.lineno}")
    assert missing == []


def test_tier3_gate_resume_is_screened_by_the_pre_review_test(tmp_path, monkeypatch):
    from agent_crew.pipeline import (PreReviewTestEnqueued, auto_enqueue_review,
                                     resume_tier3_gate)
    queue, _ = _mcp_harness(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENT_CREW_RISK_TIER_ENFORCEMENT", "1")
    queue.enqueue(TaskRequest("deploy", "implement", "deploy release", branch=BRANCH,
                              context={"risk_tier": 3, "pr_number": PR}))
    assert auto_enqueue_review(queue, "deploy", pr_number=PR, pr_state_fn=lambda *a: "open",
                               pre_review_test=True) is None
    queue.resolve_gate("risk-tier3-deploy", approved=True)
    resumed = resume_tier3_gate(queue, "risk-tier3-deploy", pr_state_fn=lambda *a: "open")
    assert resumed == PreReviewTestEnqueued(pre_review_test_id("deploy"))
    assert [r for r in _of_type(queue, "review") if r.context.get("prev_task_id") == "deploy"] == []
