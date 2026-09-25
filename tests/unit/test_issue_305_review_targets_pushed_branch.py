"""#305 — review the branch the implementer pushed, not the one the task named.

Reproduced on this host 2026-09-14. `impl-watch-0073c3a7` was ingested from
issue #304, so it carried `branch: main`, `reviewed_sha: 990ce86` (the previous
merge) and `pr_number: null`. The implementer did the normal thing: created
`fix/304-stale-pr-head-review`, pushed `9edda69`, and said so.

The cascade built the review from the implement *task*, so `review-9aeb0354` got
`branch: main` and a freshness directive telling the reviewer to run
`gh pr list --head main`. There is no such PR, the reviewer correctly returned
`request_changes` — "no reviewable live PR was found" — and that drove a fix
round against `main` for a defect no commit on `main` could fix.

⛔The deeper cause, found while implementing this: `TaskResult` had no `branch`
  and no `commit` field at all. The worker protocol asks implementers to report
  both, the endpoint accepted them, and pydantic dropped them on the floor. So
  "prefer the implementer's reported branch" was not a preference the cascade
  could express — there was nothing to prefer. The field has to exist before the
  routing can use it.

This is #304's subject one layer earlier: #304 stops a verdict being attributed
to a commit that is no longer the head; this stops a review being pointed at a
commit that never contained the work at all.
"""

import pytest

from agent_crew.protocol import TaskResult


def _result(**over):
    kwargs = dict(task_id="impl-watch-0073c3a7", status="completed", summary="done")
    kwargs.update(over)
    return TaskResult(**kwargs)


# ── 1. the result can carry where the work landed ─────────────────────


def test_a_result_carries_the_branch_it_pushed():
    """★★Without this the cascade has nothing to route on. The worker protocol
    has always asked for it; the model silently discarded it."""
    assert _result(branch="fix/304-stale-pr-head-review").branch \
        == "fix/304-stale-pr-head-review"


def test_a_result_carries_the_commit_it_pushed():
    sha = "9edda6900000000000000000000000000000abcd"
    assert _result(commit=sha).commit == sha


def test_a_result_that_reports_neither_is_empty_not_none():
    """Defaults match the module's convention for TaskRequest.branch: absent is
    `""`, so every consumer can do the same falsy check."""
    r = _result()
    assert r.branch == "" and r.commit == ""


# ── 2. a commit that is not a commit ──────────────────────────────────


@pytest.mark.parametrize("bogus", ["HEAD", "origin/main", "@", "HEAD~1", "main",
                                   "9edda69", "not a sha", "  "])
def test_a_commit_that_is_not_an_object_id_is_dropped(bogus):
    """★★My own mistake, pinned. The result for `impl-watch-0073c3a7` reported
    `commit: "HEAD"` — a literal string, useless to anything downstream.

    ⛔Abbreviations are refused too. `reviewed_sha` is compared for equality
      against a full head SHA (#304), so a 7-char prefix would never match and
      would read as "the head moved" forever.
    """
    assert _result(commit=bogus).commit == "", f"kept {bogus!r} as a commit"


@pytest.mark.parametrize("good", ["9edda6900000000000000000000000000000abcd",
                                  "A" * 40, "f" * 64])
def test_a_real_object_id_survives(good):
    """⛔The control. sha1 is 40 hex, sha256 is 64 — refusing either would drop
    every honest report."""
    assert _result(commit=good).commit == good.strip()


def test_a_bad_commit_never_throws_away_the_result():
    """⛔#270's lesson: a 422 over one field discarded an entire result. The
    field is normalised to empty, never raised on — losing a branch report is
    recoverable, losing the whole result is not."""
    r = _result(commit="HEAD", branch="fix/304-stale-pr-head-review",
                summary="the work that must not be lost")
    assert r.summary == "the work that must not be lost"
    assert r.branch == "fix/304-stale-pr-head-review"


# ── 3. the cascade routes to what was actually pushed ─────────────────


def _queue(tmp_db, task_branch="main", ctx=None):
    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue

    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(
        task_id="impl-watch-0073c3a7", task_type="implement",
        description="Implement #304", branch=task_branch,
        context=ctx if ctx is not None else {"issue": 304, "reviewed_sha": "990ce86"}))
    return q


def _review_task(q):
    return [t for t in q.list_tasks() if t.task_type == "review"][0]


def test_the_review_targets_the_pushed_branch_not_the_task_branch(tmp_db):
    """★★The incident. The task said `main`; the work is on
    `fix/304-stale-pr-head-review`."""
    from agent_crew.pipeline import auto_enqueue_review

    q = _queue(tmp_db)
    auto_enqueue_review(q, "impl-watch-0073c3a7",
                        result=_result(branch="fix/304-stale-pr-head-review"))
    assert _review_task(q).branch == "fix/304-stale-pr-head-review"


def test_the_pushed_commit_becomes_the_reviewed_sha(tmp_db):
    """The stale pin was half the reported damage: the review carried
    `990ce86`, a commit that never contained the work."""
    from agent_crew.pipeline import auto_enqueue_review

    sha = "9edda6900000000000000000000000000000abcd"
    q = _queue(tmp_db)
    auto_enqueue_review(q, "impl-watch-0073c3a7",
                        result=_result(branch="fix/304-stale-pr-head-review", commit=sha))
    assert _review_task(q).context.get("reviewed_sha") == sha


def test_a_useless_commit_does_not_overwrite_the_pin(tmp_db):
    """⛔`"HEAD"` must not become a `reviewed_sha`. Normalised away upstream, so
    the task's own pin is left alone rather than replaced with nonsense."""
    from agent_crew.pipeline import auto_enqueue_review

    q = _queue(tmp_db)
    auto_enqueue_review(q, "impl-watch-0073c3a7",
                        result=_result(branch="fix/304-stale-pr-head-review",
                                       commit="HEAD"))
    assert _review_task(q).context.get("reviewed_sha") != "HEAD"


def test_the_freshness_directive_names_the_pushed_branch(tmp_db):
    """★★The directive is what the reviewer actually follows. It told the
    reviewer to run `gh pr list --head main`, which is why it found nothing."""
    from agent_crew.pipeline import auto_enqueue_review

    q = _queue(tmp_db)
    auto_enqueue_review(q, "impl-watch-0073c3a7",
                        result=_result(branch="fix/304-stale-pr-head-review"))
    description = _review_task(q).description
    assert "fix/304-stale-pr-head-review" in description
    assert "--head main" not in description, \
        "the reviewer is still being sent to look for a PR on main"


def test_a_result_that_reports_nothing_leaves_routing_exactly_as_before(tmp_db):
    """⛔The compatibility control, and it matters more than usual here: no
    existing agent reports a branch, because the field did not exist until now.
    Every one of them must keep routing exactly as it did."""
    from agent_crew.pipeline import auto_enqueue_review

    q = _queue(tmp_db, task_branch="feat/existing")
    auto_enqueue_review(q, "impl-watch-0073c3a7", result=_result())
    assert _review_task(q).branch == "feat/existing"


def test_the_cascade_still_works_with_no_result_at_all(tmp_db):
    """The MCP path and older callers pass none. Routing must not require it."""
    from agent_crew.pipeline import auto_enqueue_review

    q = _queue(tmp_db, task_branch="feat/existing")
    auto_enqueue_review(q, "impl-watch-0073c3a7")
    assert _review_task(q).branch == "feat/existing"


def test_a_matching_branch_changes_nothing(tmp_db):
    from agent_crew.pipeline import auto_enqueue_review

    q = _queue(tmp_db, task_branch="feat/same")
    auto_enqueue_review(q, "impl-watch-0073c3a7", result=_result(branch="feat/same"))
    assert _review_task(q).branch == "feat/same"


# ── 4. the server hands the result over ───────────────────────────────


def test_the_server_routes_a_real_posted_result_to_the_pushed_branch(tmp_db, monkeypatch, *, unused_tcp_port):
    """★★The incident end to end, through the real POST path.

    ⛔This started as a source grep for `result=result`, which SURVIVED a mutant
      that deleted the argument from the call site — the same string still
      appears in the wrapper that forwards it. A grep that matches the wrong
      occurrence is not a test; drive the endpoint instead.
    """
    from fastapi.testclient import TestClient

    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue
    from agent_crew.server import create_app

    sha = "9edda6900000000000000000000000000000abcd"
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(
        task_id="impl-watch-0073c3a7", task_type="implement",
        description="Implement #304", branch="main",
        context={"issue": 304, "reviewed_sha": "990ce86"}))

    # #366: this test exercises review routing, not git reachability.  #354
    # correctly made its old made-up SHA fail closed before the cascade.  Give
    # the routing test a verified artifact; the artifact-gate suite owns the
    # negative and real-git evidence cases.
    monkeypatch.setattr(
        "agent_crew.server.verify_implement_artifact",
        lambda _task, _result, *, repo_cwd: (True, "verified artifact"),
    )

    app = create_app(db_path=tmp_db, pane_map={}, port=unused_tcp_port, watchdog_disabled=True,
                     anomaly_disabled=True, push_fn=lambda *a, **k: None)
    with TestClient(app) as client:
        response = client.post("/tasks/impl-watch-0073c3a7/result", json={
            "task_id": "impl-watch-0073c3a7", "status": "completed",
            "summary": "done", "branch": "fix/304-stale-pr-head-review",
            "commit": sha})
        assert response.status_code == 200, response.text

    reviews = [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "review"]
    assert reviews, "no review was enqueued at all"
    assert reviews[0].branch == "fix/304-stale-pr-head-review", \
        f"the review still targets {reviews[0].branch!r}"
    assert reviews[0].context.get("reviewed_sha") == sha


# ── 5. review of PR #307: the MCP transport was left behind ───────────
#
# P1. #305 wired the HTTP path and stopped. `mcp_server.submit_result` had no
# `branch`/`commit` arguments, so an MCP caller could not populate the new
# fields at all, and its implement cascade called
# `auto_enqueue_review(queue, task_id, pr_number=...)` with no `result=`.
#
# ⛔The MCP path is documented as running the SAME stage cascade as HTTP, for a
#   reason recorded in that file: "a guard on one transport is a guard an agent
#   walks around by changing how it reports" (#123). A fix applied to one
#   transport is the same shape of half-measure. My own #305 test even said "the
#   MCP path and older callers pass none" — I noticed and filed it under
#   compatibility instead of under unfinished.


def _mcp_call(mcp, tool_name, **kwargs):
    import asyncio

    func = mcp._tool_manager._tools[tool_name].fn
    if asyncio.iscoroutinefunction(func):
        return asyncio.run(func(**kwargs))
    return func(**kwargs)


def _mcp_submit(tmp_db, monkeypatch=None, **result_fields):
    """Enqueue a watch-shaped implement task and complete it over MCP."""
    from agent_crew.mcp_server import build_mcp_server
    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue

    TaskQueue(tmp_db).enqueue(TaskRequest(
        task_id="impl-watch-mcp", task_type="implement",
        description="Implement #304", branch="main",
        context={"issue": 304, "reviewed_sha": "990ce86"}))
    if monkeypatch is not None:
        # Keep the fail-closed gate in the real path.  These transport/routing
        # tests supply the gate's positive evidence at its boundary instead of
        # asking an invented SHA to be accepted by the checkout on this host.
        monkeypatch.setattr(
            "agent_crew.mcp_server.verify_implement_artifact",
            lambda _task, _result, *, repo_cwd: (True, "verified artifact"),
        )
    mcp = build_mcp_server(tmp_db)
    _mcp_call(mcp, "get_next_task", role="implementer")
    ack = _mcp_call(mcp, "submit_result", task_id="impl-watch-mcp",
                    status="completed", summary="done", **result_fields)
    assert ack.get("acknowledged") is True, ack
    reviews = [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "review"]
    return ack, reviews


def test_the_mcp_contract_accepts_the_branch_and_commit(tmp_db, monkeypatch):
    """★★An MCP caller could not report where it pushed at all — the arguments
    did not exist, so the fields could never be populated on that transport."""
    _, reviews = _mcp_submit(tmp_db, monkeypatch, branch="fix/304-stale-pr-head-review")
    assert reviews, "no review was enqueued over MCP"


def test_an_mcp_submission_routes_the_review_to_the_pushed_branch(tmp_db, monkeypatch):
    """★★The finding. An MCP-delivered watch task still reviewed `main`."""
    _, reviews = _mcp_submit(tmp_db, monkeypatch, branch="fix/304-stale-pr-head-review")
    assert reviews[0].branch == "fix/304-stale-pr-head-review"


def test_an_mcp_submission_pins_the_reviewed_sha(tmp_db, monkeypatch):
    sha = "9edda6900000000000000000000000000000abcd"
    _, reviews = _mcp_submit(tmp_db, monkeypatch, branch="fix/304-stale-pr-head-review", commit=sha)
    assert reviews[0].context.get("reviewed_sha") == sha


def test_the_mcp_transport_applies_the_same_commit_guard(tmp_db, monkeypatch):
    """⛔The normalisation lives in `TaskResult`, so both transports inherit it —
    but only if MCP actually constructs the field. Asserted through MCP so a
    future hand-rolled construction there cannot skip it."""
    _, reviews = _mcp_submit(tmp_db, monkeypatch, branch="fix/304-stale-pr-head-review",
                             commit="HEAD")
    assert reviews[0].context.get("reviewed_sha") != "HEAD"


def test_an_mcp_submission_reporting_nothing_routes_as_before(tmp_db):
    """⛔The compatibility control, same as the HTTP path's."""
    _, reviews = _mcp_submit(tmp_db)
    assert reviews[0].branch == "main"
