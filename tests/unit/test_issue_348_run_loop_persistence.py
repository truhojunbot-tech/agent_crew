"""#348: coordinator loops must retain and consume implementer ref metadata."""

from __future__ import annotations

import asyncio

import pytest
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


def _run_loop(monkeypatch, tmp_path, results, max_iter=2, branch="main", existing_tasks=None,
              queue_override=None, timeout=None, project=""):
    import agent_crew.loop as loop

    queue = queue_override or _LoopQueue(results)
    if existing_tasks is not None:
        queue.list_tasks = lambda: existing_tasks
    dispatched = []

    def enqueue_implement(queue, task, branch, context=None, port=0):
        dispatched.append(("implement", branch, context or {}))
        return f"impl-{len([x for x in dispatched if x[0] == 'implement'])}"

    def enqueue_review(queue, task, branch, prev_task_id=None, context=None, port=0):
        for existing in existing_tasks or []:
            if (existing.task_type == "review"
                    and existing.context.get("prev_task_id") == prev_task_id
                    and existing.status not in {"failed", "timed_out", "blocked"}):
                return existing.task_id
        dispatched.append(("review", branch, context or {}))
        return f"review-{len([x for x in dispatched if x[0] == 'review'])}"

    monkeypatch.setattr("agent_crew.queue.TaskQueue", lambda db: queue)
    monkeypatch.setattr(loop, "enqueue_implement", enqueue_implement)
    monkeypatch.setattr(loop, "enqueue_review", enqueue_review)
    runner = CliRunner()
    args = [
        "run", "implement persistence", "--db", str(tmp_path / "tasks.db"),
        "--branch", branch, "--no-tester", "--max-iter", str(max_iter),
    ]
    if timeout is not None:
        args += ["--timeout", str(timeout)]
    if project:
        args += ["--project", project]
    invocation = runner.invoke(crew, args)
    return invocation, dispatched


def test_run_loop_stops_after_failed_implement_without_review(tmp_path, monkeypatch):
    results = [TaskResult(task_id="impl-1", status="failed", summary="no artifact")]
    invocation, dispatched = _run_loop(monkeypatch, tmp_path, results)

    assert invocation.exit_code == 0, invocation.output
    assert [kind for kind, *_ in dispatched] == ["implement"]
    assert "ended failed" in invocation.output
    assert "server owns retry/fallback/cascade" in invocation.output


def _inflight_impl(task_id="impl-existing", *, status="pending", branch="main",
                   project="", context=None):
    return TaskRequest(task_id=task_id, task_type="implement",
                       description="implement persistence", branch=branch,
                       project=project, status=status, context=context or {})


@pytest.mark.parametrize("status", ["pending", "in_progress"])
def test_run_adopts_identical_first_implement(tmp_path, monkeypatch, status):
    results = [
        TaskResult(task_id="impl-existing", status="completed", summary="done"),
        TaskResult(task_id="review-1", status="completed", summary="approved",
                   verdict="approve", findings=[]),
    ]
    invocation, dispatched = _run_loop(
        monkeypatch, tmp_path, results, existing_tasks=[_inflight_impl(status=status)])

    assert invocation.exit_code == 0, invocation.output
    assert [kind for kind, *_ in dispatched] == ["review"]
    assert "(task already in flight, adopted impl-existing)" in invocation.output


def test_run_adopts_server_review_and_test_that_win_enqueue_race(tmp_path, monkeypatch):
    """The server can enqueue each successor after the CLI's lookup (#610)."""
    from agent_crew.queue import DuplicateReviewError

    class RacingQueue:
        project_identity = ""

        def __init__(self):
            self.tasks = {}
            self.enqueued = []

        def list_tasks(self):
            return list(self.tasks.values())

        def enqueue(self, request, **_kwargs):
            self.enqueued.append(request.task_type)
            if request.task_type in {"review", "test"}:
                server_id = f"server-{request.task_type}"
                self.tasks[server_id] = TaskRequest(
                    task_id=server_id, task_type=request.task_type,
                    description=request.description, branch=request.branch,
                    context=request.context, status="completed",
                )
                raise DuplicateReviewError(server_id)
            self.tasks[request.task_id] = request
            return request.task_id

        def get_task(self, task_id):
            return self.tasks.get(task_id)

        def get_result(self, task_id):
            if task_id.startswith("impl-"):
                return TaskResult(task_id=task_id, status="completed", summary="done",
                                  branch="main", commit=COMMIT)
            if task_id == "server-review":
                return TaskResult(task_id=task_id, status="completed", summary="approved",
                                  verdict="approve", findings=[])
            if task_id == "server-test":
                return TaskResult(task_id=task_id, status="completed", summary="passed")
            return None

        def get_task_context(self, task_id):
            return self.tasks[task_id].context

        def patch_context(self, task_id, extra):
            self.tasks[task_id].context.update(extra)

    queue = RacingQueue()
    monkeypatch.setattr("agent_crew.queue.TaskQueue", lambda _db: queue)
    result = CliRunner().invoke(crew, [
        "run", "race", "--db", str(tmp_path / "tasks.db"), "--branch", "main",
    ])

    assert result.exit_code == 0, result.output
    assert queue.enqueued == ["implement", "review", "test"]
    assert len([t for t in queue.list_tasks() if t.task_type == "review"]) == 1
    assert len([t for t in queue.list_tasks() if t.task_type == "test"]) == 1
    assert "Loop complete" in result.output


def test_run_reuses_open_pr_without_creating_another(tmp_path, monkeypatch):
    import agent_crew.loop as loop

    queue = _LoopQueue([
        TaskResult(task_id="impl-1", status="completed", summary="done"),
        TaskResult(task_id="review-1", status="completed", summary="approved",
                   verdict="approve", findings=[]),
    ])
    monkeypatch.setattr("agent_crew.queue.TaskQueue", lambda _db: queue)
    monkeypatch.setattr(loop, "enqueue_implement", lambda *_args, **_kwargs: "impl-1")
    monkeypatch.setattr(loop, "enqueue_review", lambda *_args, **_kwargs: "review-1")
    monkeypatch.setattr("agent_crew.github.check_gh_installed", lambda: True)
    monkeypatch.setattr("agent_crew.github.pr_number_for_branch", lambda *_args, **_kwargs: 609)
    monkeypatch.setattr("agent_crew.github.pr_state", lambda *_args, **_kwargs: "open")

    def no_create_pr(**_kwargs):
        raise AssertionError("existing PR must be reused")

    monkeypatch.setattr("agent_crew.github.create_pr", no_create_pr)
    result = CliRunner().invoke(crew, [
        "run", "race", "--db", str(tmp_path / "tasks.db"), "--branch", "main",
        "--no-tester", "--create-pr", "--repo", "owner/repo",
    ])

    assert result.exit_code == 0, result.output
    assert "Using existing PR #609" in result.output
    assert "Failed to create GitHub PR" not in result.output


@pytest.mark.parametrize("existing", [
    _inflight_impl(branch="other"),
    _inflight_impl(status="completed"),
    _inflight_impl(status="failed"),
    _inflight_impl(project="other"),
    _inflight_impl(context={"prev_task_id": "review-parent"}),
])
def test_run_enqueues_when_existing_implement_is_not_a_match(
        tmp_path, monkeypatch, existing):
    results = [
        TaskResult(task_id="impl-1", status="completed", summary="done"),
        TaskResult(task_id="review-1", status="completed", summary="approved",
                   verdict="approve", findings=[]),
    ]
    invocation, dispatched = _run_loop(
        monkeypatch, tmp_path, results, existing_tasks=[existing])

    assert invocation.exit_code == 0, invocation.output
    assert [kind for kind, *_ in dispatched] == ["implement", "review"]


def test_run_refuses_ambiguous_inflight_implements(tmp_path, monkeypatch):
    existing = [_inflight_impl("impl-one"),
                _inflight_impl("impl-two", status="in_progress")]
    invocation, dispatched = _run_loop(
        monkeypatch, tmp_path, [], existing_tasks=existing)

    assert invocation.exit_code == 0, invocation.output
    assert dispatched == []
    assert "impl-one" in invocation.output and "impl-two" in invocation.output


def test_run_does_not_sync_worktree_when_adopting_running_task(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_crew.cli._read_state", lambda *args: {
        "port": 0, "pane_ids": [], "worktrees": {"codex": "/unused/worktree"}})
    sync_calls = []
    monkeypatch.setattr("agent_crew.cli._sync_worktrees_to_main",
                        lambda *args, **kwargs: sync_calls.append((args, kwargs)) or {})
    results = [
        TaskResult(task_id="impl-existing", status="completed", summary="done"),
        TaskResult(task_id="review-1", status="completed", summary="approved",
                   verdict="approve", findings=[]),
    ]
    class AssertNoEarlySync(_LoopQueue):
        def get_result(self, task_id):
            if task_id == "impl-existing":
                assert sync_calls == [], "must not reset an active worktree before result"
            return super().get_result(task_id)

    invocation, dispatched = _run_loop(
        monkeypatch, tmp_path, results, project="sandbox",
        queue_override=AssertNoEarlySync(results),
        existing_tasks=[_inflight_impl(status="in_progress", project="sandbox")])

    assert invocation.exit_code == 0, invocation.output
    assert [kind for kind, *_ in dispatched] == ["review"]
    assert len(sync_calls) == 1  # normal post-completion sync remains


def test_run_syncs_worktree_before_waiting_for_adopted_pending_task(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_crew.cli._read_state", lambda *args: {
        "port": 0, "pane_ids": [], "worktrees": {"codex": "/unused/worktree"}})
    sync_calls = []
    monkeypatch.setattr("agent_crew.cli._sync_worktrees_to_main",
                        lambda *args, **kwargs: sync_calls.append((args, kwargs)) or {})
    results = [
        TaskResult(task_id="impl-existing", status="completed", summary="done"),
        TaskResult(task_id="review-1", status="completed", summary="approved",
                   verdict="approve", findings=[]),
    ]

    class AssertPendingSynced(_LoopQueue):
        def get_result(self, task_id):
            if task_id == "impl-existing":
                assert len(sync_calls) == 1, "pending worktree needs pre-run sync"
            return super().get_result(task_id)

    invocation, dispatched = _run_loop(
        monkeypatch, tmp_path, results, project="sandbox",
        queue_override=AssertPendingSynced(results),
        existing_tasks=[_inflight_impl(status="pending", project="sandbox")])

    assert invocation.exit_code == 0, invocation.output
    assert [kind for kind, *_ in dispatched] == ["review"]
    assert len(sync_calls) == 2  # pre-run sync plus normal post-completion sync


class _WaitClock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _WaitQueue:
    def __init__(self, clock, finish_at=None, status="in_progress"):
        self.clock = clock
        self.finish_at = finish_at
        self.status = status

    def get_result(self, task_id):
        if task_id == "impl-1" and self.finish_at is not None and self.clock.now >= self.finish_at:
            return TaskResult(task_id="impl-1", status="completed", summary="done")
        if task_id == "review-1":
            return TaskResult(task_id="review-1", status="completed", summary="approved",
                              verdict="approve", findings=[])
        return None

    def get_task_status(self, task_id):
        return self.status


def test_run_waits_for_server_running_without_pane_after_wrapper_deadline(tmp_path, monkeypatch,
                                                                          unused_tcp_port):
    import time
    import urllib.request

    clock = _WaitClock()
    queue = _WaitQueue(clock, finish_at=5.0)
    monkeypatch.setattr(time, "time", clock.time)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    monkeypatch.setattr("agent_crew.cli._read_state", lambda *args: {
        "port": unused_tcp_port, "pane_ids": [], "worktrees": {}})
    monkeypatch.setattr("agent_crew.cli._port_listening", lambda *args, **kwargs: True)
    monkeypatch.setattr("agent_crew.cli._verify_delivery", lambda *args, **kwargs: True)
    # The test exercises wrapper waiting, not the separate live /health check.
    monkeypatch.setattr("agent_crew.project_identity.verify_server_identity",
                        lambda *args, **kwargs: {"project": "sandbox"})
    posts = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: posts.append(args))

    invocation, dispatched = _run_loop(monkeypatch, tmp_path, [], queue_override=queue,
                                       timeout=1, project="sandbox")

    assert invocation.exit_code == 0, invocation.output
    assert [kind for kind, *_ in dispatched] == ["implement", "review"]
    assert "server still running" in invocation.output
    assert not [args for args in posts if getattr(args[0], "full_url", "").endswith(
        "/tasks/impl-1/result")]


def test_run_pending_at_wrapper_deadline_still_exits(tmp_path, monkeypatch):
    import time

    clock = _WaitClock()
    monkeypatch.setattr(time, "time", clock.time)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    invocation, dispatched = _run_loop(
        monkeypatch, tmp_path, [], queue_override=_WaitQueue(clock, status="pending"), timeout=1)

    assert invocation.exit_code == 0, invocation.output
    assert [kind for kind, *_ in dispatched] == ["implement"]
    assert "Task queued" in invocation.output


def test_run_nonrunning_without_result_keeps_wrapper_failure(tmp_path, monkeypatch):
    import time

    clock = _WaitClock()
    monkeypatch.setattr(time, "time", clock.time)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    invocation, dispatched = _run_loop(
        monkeypatch, tmp_path, [], queue_override=_WaitQueue(clock, status="failed"), timeout=1)

    assert invocation.exit_code != 0
    assert [kind for kind, *_ in dispatched] == ["implement"]
    assert "auto-failed for queue cleanup" in invocation.output


def test_run_loop_adopts_persisted_server_review(tmp_path, monkeypatch):
    review_id = "review-impl-1-r0"
    results = [
        TaskResult(task_id="impl-1", status="completed", summary="done"),
        TaskResult(task_id=review_id, status="completed", summary="approved",
                   verdict="approve", findings=[]),
    ]
    existing = [TaskRequest(task_id=review_id, task_type="review", description="review",
                            branch="main", context={"prev_task_id": "impl-1"})]
    invocation, dispatched = _run_loop(monkeypatch, tmp_path, results, existing_tasks=existing)

    assert invocation.exit_code == 0, invocation.output
    assert [kind for kind, *_ in dispatched] == ["implement"]
    assert f"Reviewing... ({review_id})" in invocation.output


def test_run_loop_enqueues_review_when_no_persisted_successor(tmp_path, monkeypatch):
    results = [
        TaskResult(task_id="impl-1", status="completed", summary="done"),
        TaskResult(task_id="review-1", status="completed", summary="approved",
                   verdict="approve", findings=[]),
    ]
    invocation, dispatched = _run_loop(monkeypatch, tmp_path, results, existing_tasks=[])

    assert invocation.exit_code == 0, invocation.output
    assert [kind for kind, *_ in dispatched] == ["implement", "review"]


def test_run_loop_ignores_dead_persisted_review(tmp_path, monkeypatch):
    results = [
        TaskResult(task_id="impl-1", status="completed", summary="done"),
        TaskResult(task_id="review-1", status="completed", summary="approved",
                   verdict="approve", findings=[]),
    ]
    existing = [TaskRequest(task_id="review-dead", task_type="review", description="review",
                            branch="main", context={"prev_task_id": "impl-1"}, status="failed")]
    invocation, dispatched = _run_loop(monkeypatch, tmp_path, results, existing_tasks=existing)

    assert invocation.exit_code == 0, invocation.output
    assert [kind for kind, *_ in dispatched] == ["implement", "review"]
    assert "Reviewing... (review-1)" in invocation.output


def test_run_loop_adopts_live_review_among_dead_reviews(tmp_path, monkeypatch):
    results = [
        TaskResult(task_id="impl-1", status="completed", summary="done"),
        TaskResult(task_id="review-live", status="completed", summary="approved",
                   verdict="approve", findings=[]),
    ]
    existing = [
        TaskRequest(task_id="review-dead", task_type="review", description="review",
                    branch="main", context={"prev_task_id": "impl-1"}, status="timed_out"),
        TaskRequest(task_id="review-live", task_type="review", description="review",
                    branch="main", context={"prev_task_id": "impl-1"}, status="pending"),
    ]
    invocation, dispatched = _run_loop(monkeypatch, tmp_path, results, existing_tasks=existing)

    assert invocation.exit_code == 0, invocation.output
    assert [kind for kind, *_ in dispatched] == ["implement"]
    assert "Reviewing... (review-live)" in invocation.output


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
    assert dispatched[0][2]["crew_run_branch"] is False
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
