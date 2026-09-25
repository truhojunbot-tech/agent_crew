"""#348: coordinator loops must retain and consume implementer ref metadata."""

from __future__ import annotations

import asyncio

from click.testing import CliRunner

from agent_crew.cli import crew
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


COMMIT = "a" * 40


def _app(db_path):
    return create_app(db_path=db_path, pane_map={}, watchdog_disabled=True,
                      anomaly_disabled=True)


def _submit_http(db_path, task_id="impl-348", stop=False, **overrides):
    body = {
        "task_id": task_id, "status": "completed", "summary": "done",
        "verdict": None, "findings": [], "pr_number": 348,
        "branch": "fix/348-result", "commit": COMMIT,
    }
    body.update(overrides)
    TaskQueue(db_path).enqueue(TaskRequest(
        task_id=task_id, task_type="implement", description="work", branch="main",
        priority=3, context={"coordinator_managed": True},
    ))
    if stop:
        TaskQueue(db_path).set_stop_epoch(True, incident="issue-348")

    async def submit():
        app = _app(db_path)
        async with app.router.lifespan_context(app):
            handler = next(route.endpoint for route in app.routes
                           if getattr(route, "path", "") == "/tasks/{task_id}/result")
            return handler(task_id, TaskResult(**body))

    return asyncio.run(submit())


def test_http_result_persists_ref_and_get_result_returns_it(tmp_path):
    db_path = str(tmp_path / "tasks.db")
    response = _submit_http(db_path)

    assert response["status"] == "ok"
    result = TaskQueue(db_path).get_result("impl-348")
    assert result is not None
    assert (result.pr_number, result.branch, result.commit) == (348, "fix/348-result", COMMIT)


def test_http_result_succeeds_when_ref_context_persistence_fails(tmp_path, monkeypatch):
    db_path = str(tmp_path / "tasks.db")

    def broken_merge(*args, **kwargs):
        raise OSError("disk hiccup")

    monkeypatch.setattr(TaskQueue, "merge_task_context", broken_merge)
    response = _submit_http(db_path)

    assert response["status"] == "ok"
    assert TaskQueue(db_path).get_result("impl-348") is not None


def test_result_without_ref_metadata_remains_empty(tmp_path):
    db_path = str(tmp_path / "tasks.db")
    response = _submit_http(db_path, pr_number=None, branch="", commit="")

    assert response["status"] == "ok"
    result = TaskQueue(db_path).get_result("impl-348")
    assert result is not None
    assert (result.pr_number, result.branch, result.commit) == (None, "", "")


def test_paused_http_submit_keeps_existing_suppression_and_ref_persistence_is_safe(tmp_path):
    db_path = str(tmp_path / "tasks.db")
    response = _submit_http(db_path, stop=True)

    assert response["suppressed_by_pause"] is True
    result = TaskQueue(db_path).get_result("impl-348")
    assert result is not None
    assert (result.branch, result.commit) == ("fix/348-result", COMMIT)


def test_retry_child_drops_parent_result_refs_and_empty_result_stays_empty(tmp_path):
    db_path = str(tmp_path / "tasks.db")
    TaskQueue(db_path).enqueue(TaskRequest(
        task_id="impl-parent", task_type="implement", description="work", branch="main",
        priority=3, context={"result_branch": "fix/stale", "result_commit": COMMIT},
    ))

    async def submit_failed_parent():
        app = _app(db_path)
        async with app.router.lifespan_context(app):
            handler = next(route.endpoint for route in app.routes
                           if getattr(route, "path", "") == "/tasks/{task_id}/result")
            return handler("impl-parent", TaskResult(
                task_id="impl-parent", status="failed", summary="ordinary failure",
            ))

    asyncio.run(submit_failed_parent())
    queue = TaskQueue(db_path)
    child = next(task for task in queue.list_tasks() if task.task_id.startswith("retry-"))
    assert "result_branch" not in child.context
    assert "result_commit" not in child.context
    queue.submit_result(child.task_id, TaskResult(
        task_id=child.task_id, status="completed", summary="child done",
    ))
    result = queue.get_result(child.task_id)
    assert result is not None
    assert (result.branch, result.commit) == ("", "")


def test_mcp_submit_persists_result_refs_fail_soft(tmp_path, monkeypatch):
    import agent_crew.mcp_server as mcp_server

    db_path = str(tmp_path / "tasks.db")
    queue = TaskQueue(db_path)
    queue.enqueue(TaskRequest(task_id="mcp-348", task_type="discuss", description="work",
                              branch="main", context={}))
    tool = mcp_server.build_mcp_server(db_path)._tool_manager._tools["submit_result"].fn
    ack = asyncio.run(tool(task_id="mcp-348", summary="done", branch="fix/mcp", commit=COMMIT)) \
        if asyncio.iscoroutinefunction(tool) else tool(
            task_id="mcp-348", summary="done", branch="fix/mcp", commit=COMMIT)
    assert ack["acknowledged"] is True
    persisted = TaskQueue(db_path).get_result("mcp-348")
    assert persisted is not None
    assert (persisted.branch, persisted.commit) == ("fix/mcp", COMMIT)

    monkeypatch.setattr(TaskQueue, "merge_task_context", lambda *args: (_ for _ in ()).throw(OSError("disk")))
    queue.enqueue(TaskRequest(task_id="mcp-fail-soft", task_type="discuss", description="work",
                              branch="main", context={}))
    ack = asyncio.run(tool(task_id="mcp-fail-soft", summary="done", branch="fix/mcp", commit=COMMIT)) \
        if asyncio.iscoroutinefunction(tool) else tool(
            task_id="mcp-fail-soft", summary="done", branch="fix/mcp", commit=COMMIT)
    assert ack["acknowledged"] is True


class _LoopQueue:
    def __init__(self, results):
        self.results = iter(results)

    def get_result(self, task_id):
        return next(self.results)


def _run_loop(monkeypatch, tmp_path, results, max_iter=2, branch="main"):
    import agent_crew.loop as loop

    queue = _LoopQueue(results)
    dispatched = []

    def enqueue_implement(queue, task, branch, context=None, port=0):
        dispatched.append(("implement", branch, context or {}))
        return f"impl-{len([x for x in dispatched if x[0] == 'implement'])}"

    def enqueue_review(queue, task, branch, prev_task_id=None, context=None, port=0):
        dispatched.append(("review", branch, context or {}))
        return f"review-{len([x for x in dispatched if x[0] == 'review'])}"

    monkeypatch.setattr("agent_crew.queue.TaskQueue", lambda db: queue)
    monkeypatch.setattr(loop, "enqueue_implement", enqueue_implement)
    monkeypatch.setattr(loop, "enqueue_review", enqueue_review)
    runner = CliRunner()
    invocation = runner.invoke(crew, [
        "run", "implement persistence", "--db", str(tmp_path / "tasks.db"),
        "--branch", branch, "--no-tester", "--max-iter", str(max_iter),
    ])
    return invocation, dispatched


def test_run_loop_reviews_and_reimplements_on_reported_ref(tmp_path, monkeypatch):
    results = [
        TaskResult(task_id="impl-1", status="completed", summary="done", pr_number=348,
                   branch="fix/348-result", commit=COMMIT),
        TaskResult(task_id="review-1", status="completed", summary="changes",
                   verdict="request_changes", findings=["fix it"]),
        TaskResult(task_id="impl-2", status="completed", summary="done",
                   branch="fix/348-result", commit="b" * 40),
        TaskResult(task_id="review-2", status="completed", summary="approved",
                   verdict="approve", findings=[]),
    ]
    invocation, dispatched = _run_loop(monkeypatch, tmp_path, results)

    assert invocation.exit_code == 0, invocation.output
    assert dispatched[1] == ("review", "fix/348-result", {
        "coordinator_managed": True, "no_tester": True, "pr_number": 348,
        "reviewed_sha": COMMIT,
    })
    assert dispatched[2][0:2] == ("implement", "fix/348-result")


def test_run_loop_stops_when_implementer_reports_same_commit_twice(tmp_path, monkeypatch):
    results = [
        TaskResult(task_id="impl-1", status="completed", summary="done",
                   branch="fix/348-result", commit=COMMIT),
        TaskResult(task_id="review-1", status="completed", summary="changes",
                   verdict="request_changes", findings=["fix it"]),
        TaskResult(task_id="impl-2", status="completed", summary="done",
                   branch="fix/348-result", commit=COMMIT),
    ]
    invocation, dispatched = _run_loop(monkeypatch, tmp_path, results)

    assert invocation.exit_code == 0, invocation.output
    assert "implementer changes not persisting (same commit reported twice)" in invocation.output
    assert [kind for kind, *_ in dispatched] == ["implement", "review", "implement"]


def test_run_loop_carries_last_nonempty_branch_when_next_result_omits_one(tmp_path, monkeypatch):
    results = [
        TaskResult(task_id="impl-1", status="completed", summary="done",
                   branch="fix/348-result", commit=COMMIT),
        TaskResult(task_id="review-1", status="completed", summary="changes",
                   verdict="request_changes", findings=["fix it"]),
        TaskResult(task_id="impl-2", status="completed", summary="done", commit="b" * 40),
        TaskResult(task_id="review-2", status="completed", summary="approved",
                   verdict="approve", findings=[]),
    ]
    invocation, dispatched = _run_loop(monkeypatch, tmp_path, results)

    assert invocation.exit_code == 0, invocation.output
    assert dispatched[3][0:2] == ("review", "fix/348-result")


def test_run_branch_propagates_to_implementer_and_reviewer(tmp_path, monkeypatch):
    branch = "fix/348-target"
    results = [
        TaskResult(task_id="impl-1", status="completed", summary="done", commit=COMMIT),
        TaskResult(task_id="review-1", status="completed", summary="approved",
                   verdict="approve", findings=[]),
    ]
    invocation, dispatched = _run_loop(monkeypatch, tmp_path, results, branch=branch)

    assert invocation.exit_code == 0, invocation.output
    assert dispatched[0][0:2] == ("implement", branch)
    assert dispatched[0][2]["base_branch"] == "main"
    assert dispatched[0][2]["crew_run_branch"] is True
    assert dispatched[1][0:2] == ("review", branch)
    assert dispatched[1][2]["reviewed_sha"] == COMMIT


def test_new_commit_on_same_branch_does_not_trigger_identical_guard(tmp_path, monkeypatch):
    branch = "fix/348-target"
    next_commit = "b" * 40
    results = [
        TaskResult(task_id="impl-1", status="completed", summary="done",
                   branch=branch, commit=COMMIT),
        TaskResult(task_id="review-1", status="completed", summary="changes",
                   verdict="request_changes", findings=["fix it"]),
        TaskResult(task_id="impl-2", status="completed", summary="done",
                   branch=branch, commit=next_commit),
        TaskResult(task_id="review-2", status="completed", summary="approved",
                   verdict="approve", findings=[]),
    ]
    invocation, dispatched = _run_loop(monkeypatch, tmp_path, results, branch=branch)

    assert invocation.exit_code == 0, invocation.output
    assert "same commit reported twice" not in invocation.output
    assert dispatched[2][2]["base_branch"] == "main"
    assert dispatched[2][2]["crew_run_branch"] is True
    assert dispatched[3][2]["reviewed_sha"] == next_commit


def test_run_branch_refuses_result_from_another_branch(tmp_path, monkeypatch):
    results = [TaskResult(task_id="impl-1", status="completed", summary="done",
                          branch="fix/wrong", commit=COMMIT)]
    invocation, dispatched = _run_loop(monkeypatch, tmp_path, results,
                                       branch="fix/348-target")

    assert "reported branch 'fix/wrong'" in invocation.output
    assert [kind for kind, *_ in dispatched] == ["implement"]
