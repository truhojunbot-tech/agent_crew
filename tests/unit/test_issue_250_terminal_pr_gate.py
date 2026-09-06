"""#250 — the cascade must stop at a terminal PR, not just at the round cap.

PR #241 merged at 2026-09-02T01:42:20Z. Agent Crew went on completing reviews
and posting "automated fix rounds exhausted" to it until 15:30Z — 13.8 hours of
provider work that could not reach the artifact, across 25 exhaustion comments,
seven of them inside three minutes.

The round budget was working exactly as designed. It bounds ONE lineage; it has
nothing to say about whether the lineage is still worth anything. That is a
separate question with a separate answer:

  * merged/closed PR → no new provider work, and no escalation comment;
  * the result still persists — auditability is not what we are trimming;
  * an unverifiable PR state defers rather than guesses;
  * an open PR behaves exactly as before.
"""

import uuid

import pytest

from agent_crew.pipeline import (
    FIX_EXHAUSTED_MARKER,
    auto_enqueue_fix,
    auto_enqueue_review,
    auto_enqueue_test,
    pr_is_actionable,
)
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue

BRANCH = "agent/claude/239-context-pack"
#: ⛔Deliberately NOT a real PR. This file used 241 — a real, merged PR — and
#: drove the result handler, which posts a comment for any review result with a
#: pr_number. 228 of PR #241's 263 comments turned out to be this file's
#: fixtures (#263). The number below cannot exist in the repo, and the suite-wide
#: block in conftest is the belt to this brace.
PR = 999241
FINDING = "HIGH src/agent_crew/context_pack.py:759 - the cap drops the AC"


def _state(value):
    return lambda pr: value


@pytest.fixture
def q(tmp_db):
    return TaskQueue(tmp_db)


def _review(q, *, verdict="request_changes", findings=(FINDING,), pr_number=PR,
            context=None, summary="request_changes: fix the cap"):
    rid = f"review-{uuid.uuid4().hex[:8]}"
    ctx = {"prev_task_id": "impl-1", "pr_number": pr_number}
    ctx.update(context or {})
    q.enqueue(TaskRequest(task_id=rid, task_type="review", description="review",
                          branch=BRANCH, context=ctx))
    q.submit_result(rid, TaskResult(task_id=rid, status="completed", summary=summary,
                                    verdict=verdict, findings=list(findings),
                                    pr_number=pr_number))
    return rid


def _implement(q, pr_number=PR):
    iid = f"impl-{uuid.uuid4().hex[:8]}"
    q.enqueue(TaskRequest(task_id=iid, task_type="implement", description="impl",
                          branch=BRANCH, context={"pr_number": pr_number}))
    q.submit_result(iid, TaskResult(task_id=iid, status="completed", summary="done",
                                    pr_number=pr_number))
    return iid


# ── 1. the gate itself ────────────────────────────────────────────────


@pytest.mark.parametrize("state,actionable", [
    ("open", True), ("merged", False), ("closed", False), ("unknown", False),
])
def test_only_an_open_pr_is_actionable(state, actionable):
    assert pr_is_actionable(PR, pr_state_fn=_state(state)) == (actionable, state)


def test_a_task_with_no_pr_is_still_actionable():
    """⛔"No PR" is not "terminal PR". The first implement→review hop happens
    before any PR exists, and gating it would break the normal pipeline."""
    assert pr_is_actionable(None, pr_state_fn=_state("merged")) == (True, "no_pr")
    assert pr_is_actionable(0, pr_state_fn=_state("merged"))[0] is True


def test_a_raising_lookup_is_unknown_not_open():
    def boom(pr):
        raise RuntimeError("github is down")

    assert pr_is_actionable(PR, pr_state_fn=boom) == (False, "unknown")


# ── 2. no new provider work for a terminal PR ─────────────────────────


@pytest.mark.parametrize("state", ["merged", "closed"])
def test_request_changes_on_a_terminal_pr_enqueues_no_fix(q, state):
    """★The reported bug: #241 merged, and fixes kept being spawned for it."""
    review_id = _review(q)

    assert auto_enqueue_fix(q, review_id, pr_state_fn=_state(state)) is None
    assert not [t for t in q.list_tasks() if t.task_type == "implement"]


def test_a_merged_pr_stops_the_review_cascade(q):
    impl_id = _implement(q)

    assert auto_enqueue_review(q, impl_id, PR, pr_state_fn=_state("merged")) is None
    assert not [t for t in q.list_tasks() if t.task_type == "review"]


def test_a_merged_pr_stops_the_test_cascade(q):
    review_id = _review(q, verdict="approve", findings=[], summary="lgtm")

    assert auto_enqueue_test(q, review_id, pr_state_fn=_state("merged")) is None
    assert not [t for t in q.list_tasks() if t.task_type == "test"]


def test_an_unverifiable_pr_state_defers_rather_than_guesses(q):
    """⛔GitHub unreachable → no NEW work.

    The asymmetry is the argument, and it is not a general fail-closed policy:
    a skipped cascade is recoverable (the result is persisted; a human or a
    later task resumes it), while work spawned against a merged PR is spend
    that can never be recovered.
    """
    review_id = _review(q)

    assert auto_enqueue_fix(q, review_id, pr_state_fn=_state("unknown")) is None
    assert not [t for t in q.list_tasks() if t.task_type == "implement"]


def test_a_late_duplicate_result_after_merge_still_creates_nothing(q):
    """The observed shape: one lineage reporting repeatedly after the merge."""
    review_id = _review(q)

    for _ in range(3):
        assert auto_enqueue_fix(q, review_id, pr_state_fn=_state("merged")) is None
    assert not [t for t in q.list_tasks() if t.task_type == "implement"]


# ── 3. no misleading escalation on a terminal PR ──────────────────────


def test_no_exhaustion_comment_is_posted_to_a_terminal_pr(q, monkeypatch):
    """★10 of the 25 comments on #241 landed after it merged, each telling a
    human to decide something about a PR that was already decided."""
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "3")
    posted = []
    review_id = _review(q, context={"fix_round": 3})

    auto_enqueue_fix(q, review_id, pr_state_fn=_state("merged"),
                     comment_fn=lambda pr, body: posted.append((pr, body)))

    assert posted == [], "an exhaustion notice was posted to a terminal PR"


def test_the_exhaustion_notice_is_said_once_per_pr(q, monkeypatch):
    """★The amplifier: every late result whose lineage was already over budget
    posted the same message again — seven within three minutes on #241."""
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "3")
    posted = []
    already = {"seen": False}

    def announced(pr, marker):
        assert marker == FIX_EXHAUSTED_MARKER
        return already["seen"]

    def comment(pr, body):
        posted.append(body)
        already["seen"] = FIX_EXHAUSTED_MARKER in body

    for _ in range(4):          # four distinct late results, same PR
        auto_enqueue_fix(q, _review(q, context={"fix_round": 3}),
                         pr_state_fn=_state("open"),
                         already_announced_fn=announced, comment_fn=comment)

    assert len(posted) == 1, f"posted {len(posted)} identical exhaustion notices"
    assert FIX_EXHAUSTED_MARKER in posted[0]


def test_an_unverifiable_comment_check_still_posts(q, monkeypatch):
    """⛔Deliberately the opposite bias from the work gate, and for a reason: a
    duplicate comment costs nothing, while a MISSING escalation leaves a PR
    looking like it is still being worked. Cheap-and-noisy beats silent here;
    the reverse holds when the alternative is provider spend."""
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "3")
    posted = []

    auto_enqueue_fix(q, _review(q, context={"fix_round": 3}),
                     pr_state_fn=_state("open"),
                     already_announced_fn=lambda pr, marker: None,   # cannot tell
                     comment_fn=lambda pr, body: posted.append(body))

    assert len(posted) == 1


# ── 4. the open-PR path is untouched ──────────────────────────────────


def test_an_open_pr_still_cascades_normally(q):
    """⛔The gate must not become a general brake on the pipeline."""
    review_id = _review(q)

    fix_id = auto_enqueue_fix(q, review_id, pr_state_fn=_state("open"))

    assert fix_id is not None
    fix = {t.task_id: t for t in q.list_tasks()}[fix_id]
    assert fix.task_type == "implement" and FINDING in fix.description


def test_an_open_pr_still_gets_review_and_test_cascades(q):
    impl_id = _implement(q)
    assert auto_enqueue_review(q, impl_id, PR, pr_state_fn=_state("open")) is not None

    review_id = _review(q, verdict="approve", findings=[], summary="lgtm")
    assert auto_enqueue_test(q, review_id, pr_state_fn=_state("open")) is not None


# ── 5. end to end through the real result handler ─────────────────────


def _server(tmp_db, push):
    from agent_crew.server import create_app

    return create_app(db_path=tmp_db,
                      pane_map={"implementer": "%1", "reviewer": "%2", "tester": "%3"},
                      port=8105, push_fn=push, watchdog_disabled=True,
                      anomaly_disabled=True)


class _Push:
    def __init__(self):
        self.calls = []

    def __call__(self, pane, text):
        self.calls.append((pane, text))


def _enqueue_review(c, task_id):
    return c.post("/tasks", json={"task_id": task_id, "task_type": "review",
                                  "description": "review", "branch": BRANCH,
                                  "priority": 3, "context": {"pr_number": PR},
                                  "project": ""})


def _result(c, task_id):
    return c.post(f"/tasks/{task_id}/result",
                  json={"task_id": task_id, "status": "completed",
                        "summary": "request_changes: still broken",
                        "verdict": "request_changes", "findings": [FINDING],
                        "pr_number": PR})


def test_merge_during_an_in_flight_review_creates_no_follow_up(tmp_db, monkeypatch, github_writes):
    """★★The race #250 describes, driven through POST /tasks/{id}/result.

    The review was dispatched while the PR was open and reports back after the
    merge — nothing in the task itself says the world moved. The handler has to
    notice, and it has to keep the result.
    """
    from fastapi.testclient import TestClient

    monkeypatch.setattr("agent_crew.github.pr_state", lambda pr, *a, **k: "merged")
    monkeypatch.setattr("agent_crew.github.post_pr_comment",
                        lambda *a, **k: pytest.fail("commented on a merged PR"))
    push = _Push()
    with TestClient(_server(tmp_db, push)) as c:
        _enqueue_review(c, "review-late")
        assert _result(c, "review-late").status_code == 200
        # ⛔The result stays durable and auditable — that is not what we trim.
        stored = c.get("/tasks/review-late").json()
        assert stored["status"] == "completed"
        assert stored["verdict"] == "request_changes"
        assert stored["findings"] == [FINDING]

    q = TaskQueue(tmp_db)
    assert not [t for t in q.list_tasks() if t.task_type == "implement"], \
        "a fix task was spawned for a merged PR"
    assert not [p for p, _ in push.calls if p == "%1"], \
        "work was pushed to an agent for a merged PR"


def test_an_open_pr_still_produces_a_fix_through_the_handler(tmp_db, monkeypatch, github_writes):
    """The same path with the PR open — what #244 shipped must survive."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr("agent_crew.github.pr_state", lambda pr, *a, **k: "open")
    push = _Push()
    with TestClient(_server(tmp_db, push)) as c:
        _enqueue_review(c, "review-open")
        _result(c, "review-open")

    fixes = [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "implement"]
    assert len(fixes) == 1 and FINDING in fixes[0].description


def test_a_github_outage_during_the_handler_loses_nothing(tmp_db, monkeypatch, github_writes):
    """Result durable, cascade deferred — the two halves of #250's req 3."""
    from fastapi.testclient import TestClient

    def down(*a, **k):
        raise RuntimeError("gh: network unreachable")

    monkeypatch.setattr("agent_crew.github.pr_state", down)
    push = _Push()
    with TestClient(_server(tmp_db, push)) as c:
        _enqueue_review(c, "review-outage")
        assert _result(c, "review-outage").status_code == 200
        assert c.get("/tasks/review-outage").json()["status"] == "completed"

    assert not [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "implement"]


# ── 6. the PR a task is ABOUT vs the PR its result names ──────────────
#
# Both transports gate on `result.pr_number`, and an agent may simply omit it —
# while the task context has carried the PR since the task was created. Gating
# on the reported value alone read "no PR" for a task whose PR was merged and
# queued a review of a closed artifact (review of PR #251).


def _impl_with_context_pr(q, task_id="impl-ctx", pr=PR, reported=None, branch=BRANCH):
    q.enqueue(TaskRequest(task_id=task_id, task_type="implement", description="impl",
                          branch=branch, context={"pr_number": pr} if pr else {}))
    q.submit_result(task_id, TaskResult(task_id=task_id, status="completed",
                                        summary="done", pr_number=reported))
    return task_id


def test_a_result_that_omits_the_pr_still_hits_the_gate(q):
    """★The bypass: the PR is merged, the result is silent, the context knows."""
    impl_id = _impl_with_context_pr(q)

    assert auto_enqueue_review(q, impl_id, None, pr_state_fn=_state("merged")) is None
    assert not [t for t in q.list_tasks() if t.task_type == "review"]


def test_the_resolved_pr_reaches_the_review_context(q):
    """⛔Not just the gate. A review created with `pr_number: None` leaves the
    NEXT hop blind for the same reason, so the resolved value has to be written
    into the context the cascade will read later."""
    impl_id = _impl_with_context_pr(q)

    review_id = auto_enqueue_review(q, impl_id, None, pr_state_fn=_state("open"))

    review = {t.task_id: t for t in q.list_tasks()}[review_id]
    assert review.context["pr_number"] == PR
    assert f"PR #{PR}" in review.context["instructions"]


def test_an_explicitly_reported_pr_wins_over_the_context(q):
    """The result is the fresher fact when it has one."""
    impl_id = _impl_with_context_pr(q, pr=PR, reported=999)

    review_id = auto_enqueue_review(q, impl_id, 999, pr_state_fn=_state("open"))

    assert {t.task_id: t for t in q.list_tasks()}[review_id].context["pr_number"] == 999


def test_a_string_pr_number_in_context_is_still_resolved(q):
    """Contexts are JSON round-tripped by several producers; "241" is a PR."""
    q.enqueue(TaskRequest(task_id="impl-str", task_type="implement", description="impl",
                          branch=BRANCH, context={"pr_number": "241"}))
    q.submit_result("impl-str", TaskResult(task_id="impl-str", status="completed",
                                           summary="done"))

    assert auto_enqueue_review(q, "impl-str", None, pr_state_fn=_state("merged")) is None


def test_a_task_with_no_pr_anywhere_still_cascades(q):
    """⛔The resolution must not invent a PR. A task that genuinely has none is
    still actionable — that is the pre-PR implement→review hop."""
    impl_id = _impl_with_context_pr(q, task_id="impl-nopr", pr=None)

    assert auto_enqueue_review(q, impl_id, None, pr_state_fn=_state("merged")) is not None


def test_http_result_omitting_the_pr_does_not_review_a_merged_pr(tmp_db, monkeypatch):
    """★★The regression through the real handler: `POST /tasks/{id}/result`
    passes `result.pr_number`, which is exactly what an agent may omit."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr("agent_crew.github.pr_state", lambda pr, *a, **k: "merged")
    push = _Push()
    with TestClient(_server(tmp_db, push)) as c:
        c.post("/tasks", json={"task_id": "impl-http", "task_type": "implement",
                               "description": "impl", "branch": BRANCH, "priority": 3,
                               "context": {"pr_number": PR}, "project": ""})
        r = c.post("/tasks/impl-http/result",
                   json={"task_id": "impl-http", "status": "completed",
                         "summary": "done", "verdict": None, "findings": [],
                         "pr_number": None})            # the agent omits it
        assert r.status_code == 200
        assert c.get("/tasks/impl-http").json()["status"] == "completed"

    assert not [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "review"], \
        "a review was queued for a merged PR because the result omitted pr_number"


def test_mcp_result_omitting_the_pr_does_not_review_a_merged_pr(tmp_db, monkeypatch):
    """★★Same regression on the MCP transport, which passes the same value."""
    import asyncio

    from agent_crew.mcp_server import build_mcp_server

    monkeypatch.setattr("agent_crew.github.pr_state", lambda pr, *a, **k: "merged")
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-mcp", task_type="implement", description="impl",
                          branch=BRANCH, context={"pr_number": PR}))
    q.dequeue(role="implementer")

    mcp = build_mcp_server(tmp_db)
    fn = mcp._tool_manager._tools["submit_result"].fn
    kwargs = dict(task_id="impl-mcp", status="completed", summary="done")
    out = asyncio.run(fn(**kwargs)) if asyncio.iscoroutinefunction(fn) else fn(**kwargs)

    assert out.get("acknowledged") is True, out
    assert not [t for t in q.list_tasks() if t.task_type == "review"]


def test_http_result_omitting_the_pr_still_reviews_an_open_pr(tmp_db, monkeypatch):
    """⛔And the resolution must not brake the normal path."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr("agent_crew.github.pr_state", lambda pr, *a, **k: "open")
    push = _Push()
    with TestClient(_server(tmp_db, push)) as c:
        c.post("/tasks", json={"task_id": "impl-open", "task_type": "implement",
                               "description": "impl", "branch": BRANCH, "priority": 3,
                               "context": {"pr_number": PR}, "project": ""})
        c.post("/tasks/impl-open/result",
               json={"task_id": "impl-open", "status": "completed", "summary": "done",
                     "verdict": None, "findings": [], "pr_number": None})

    reviews = [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "review"]
    assert len(reviews) == 1
    assert reviews[0].context["pr_number"] == PR, \
        "the review must carry the resolved PR so the next hop is gated too"


# ── 7. the reviewer must read the PR, not whatever `gh` guesses ───────
#
# Found while investigating why three consecutive reviews of PR #251 reported
# "the PR is unchanged" against a branch whose fix was already pushed. The
# server log said it every time:
#
#   WARNING: could not resolve PR #251 head for reviewer review-65d3a728
#            — falling back to task.branch='main'
#
# `_resolve_pr_head_branch` shelled out to `gh` with no cwd, so `gh` resolved
# the repository from the SERVER process's working directory — the instance
# directory, which is not a checkout. Resolution failed, prep fell back to the
# task's base branch, and the reviewer read `main`: code the PR does not
# contain. A review of the wrong tree looks exactly like a review.


def test_pr_head_resolution_runs_inside_the_worktree(monkeypatch):
    """★The fix: `gh` must be asked from a checkout of the right repository."""
    import subprocess as sp

    from agent_crew import server as sv

    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["cwd"] = kw.get("cwd")

        class R:
            returncode = 0
            stdout = "fix/250-terminal-pr-gate\n"
            stderr = ""

        return R()

    monkeypatch.setattr(sv.subprocess, "run", fake_run)

    assert sv._resolve_pr_head_branch(251, cwd="/wt/codex") == "fix/250-terminal-pr-gate"
    assert seen["cwd"] == "/wt/codex", \
        "gh was asked from the server's cwd, so it resolves the wrong repository"
    assert "251" in seen["argv"]


def test_worktree_prep_asks_from_the_worktree_being_prepared(monkeypatch, tmp_path):
    """The seam only helps if the caller actually passes the worktree."""
    from agent_crew import server as sv

    calls = []
    monkeypatch.setattr(sv, "_resolve_pr_head_branch",
                        lambda pr, cwd=None: calls.append((pr, cwd)) or "feat/x")
    monkeypatch.setattr(sv.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "",
                                                       "stderr": ""})())

    sv._prepare_worktree_for_task_inner(
        str(tmp_path), "review-abc", "main", "reviewer", {"pr_number": 251})

    assert calls == [(251, str(tmp_path))]


def test_an_unresolvable_pr_head_is_logged_as_an_error(monkeypatch, tmp_path, caplog):
    """⛔The fallback reviews a ref that is NOT the PR. Nothing downstream says
    so, so the log has to — at ERROR, not tucked into a warning stream."""
    import logging

    from agent_crew import server as sv

    monkeypatch.setattr(sv, "_resolve_pr_head_branch", lambda pr, cwd=None: None)
    monkeypatch.setattr(sv.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "",
                                                       "stderr": ""})())

    with caplog.at_level(logging.ERROR, logger="agent_crew.server"):
        sv._prepare_worktree_for_task_inner(
            str(tmp_path), "review-abc", "main", "reviewer", {"pr_number": 251})

    errors = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("could not resolve PR #251" in m for m in errors)
    assert any("MAY NOT BE THE PR'S CODE" in m for m in errors)


# ── 8. the exhaustion notice needs an atomic claim, not a check ───────
#
# The once-per-PR notice asked GitHub "is it already there?" and then posted.
# That is check-then-act: two results completing together both read "no" and
# both post. Reproduced before fixing — two notices for one PR — and the
# original sequential test could not see it, because it mutated an in-memory
# flag between calls (review of PR #251, round 2).
#
# The same reasoning made the fix task id derived rather than checked in #244.
# The arbiter is a row with a PRIMARY KEY, so exactly one caller wins across
# threads and processes sharing the database.


def _exhausted_review(q, pr=PR):
    return _review(q, context={"fix_round": 3}, pr_number=pr)


def test_concurrent_exhausted_results_post_one_notice(tmp_db, monkeypatch):
    """★The race. Four results finish together; the PR gets one notice."""
    import threading

    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "3")
    seed = TaskQueue(tmp_db)
    ids = [_exhausted_review(seed) for _ in range(4)]

    posted, lock, start = [], threading.Lock(), threading.Barrier(len(ids))
    # ⛔The check has to happen for every caller that reaches it BEFORE any of
    #   them posts — that is the interleaving, and a test that lets the first
    #   caller post before the others look is the toothless shape the review
    #   rejected. The timeout is what keeps this honest in both directions:
    #   with the claim in place only one caller gets here and simply proceeds.
    checked = threading.Barrier(len(ids))

    def announced(pr, marker):
        with lock:
            seen = any(marker in b for b in posted)
        try:
            checked.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass
        return seen

    def comment(pr, body):
        with lock:
            posted.append(body)

    def go(rid):
        start.wait()
        auto_enqueue_fix(TaskQueue(tmp_db), rid,      # own connection, as in prod
                         pr_state_fn=_state("open"),
                         already_announced_fn=announced, comment_fn=comment)

    threads = [threading.Thread(target=go, args=(r,)) for r in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(posted) == 1, f"{len(posted)} notices published for one PR"


def test_the_claim_is_durable_across_connections(tmp_db, monkeypatch):
    """A restarted process must not re-announce: the claim is on disk, not in
    memory, which is the other half of what the in-memory flag could not do."""
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "3")
    posted = []

    auto_enqueue_fix(TaskQueue(tmp_db), _exhausted_review(TaskQueue(tmp_db)),
                     pr_state_fn=_state("open"),
                     already_announced_fn=lambda pr, m: False,
                     comment_fn=lambda pr, body: posted.append(body))
    # ...a fresh TaskQueue, as a restarted server would build:
    auto_enqueue_fix(TaskQueue(tmp_db), _exhausted_review(TaskQueue(tmp_db)),
                     pr_state_fn=_state("open"),
                     already_announced_fn=lambda pr, m: False,
                     comment_fn=lambda pr, body: posted.append(body))

    assert len(posted) == 1


def test_a_failed_post_gives_the_claim_back(tmp_db, monkeypatch):
    """⛔A claim held after a failed post would suppress the escalation
    permanently — the PR goes quiet, which is what the notice exists to
    prevent. The retry must be able to win the claim again."""
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "3")
    q = TaskQueue(tmp_db)
    posted = []

    def boom(pr, body):
        raise RuntimeError("gh is down")

    auto_enqueue_fix(q, _exhausted_review(q), pr_state_fn=_state("open"),
                     already_announced_fn=lambda pr, m: False, comment_fn=boom)
    assert q.pr_announcement_state(PR, "fix_exhausted") is None, "claim not released"

    auto_enqueue_fix(q, _exhausted_review(q), pr_state_fn=_state("open"),
                     already_announced_fn=lambda pr, m: False,
                     comment_fn=lambda pr, body: posted.append(body))
    assert len(posted) == 1, "the retry could not reclaim the announcement"


def test_a_false_return_from_post_pr_comment_gives_the_claim_back(tmp_db, monkeypatch):
    """⛔`post_pr_comment` reports failure by returning False, not by
    raising (gh missing, repo unresolved, non-zero exit all return False).
    The default path — no comment_fn override — must treat a False return
    exactly like an exception: release the claim rather than mark it
    posted for a comment that never reached the PR."""
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "3")
    monkeypatch.setattr("agent_crew.github.post_pr_comment", lambda *a, **k: False)
    q = TaskQueue(tmp_db)

    auto_enqueue_fix(q, _exhausted_review(q), pr_state_fn=_state("open"),
                     already_announced_fn=lambda pr, m: False)
    assert q.pr_announcement_state(PR, "fix_exhausted") is None, \
        "a False return was treated as success -- claim was not released"


def test_a_posted_claim_is_never_released(tmp_db):
    """Releasing a posted claim would re-open the very race it closed."""
    q = TaskQueue(tmp_db)
    token = q.claim_pr_announcement(PR, "fix_exhausted")
    assert token
    q.mark_pr_announcement_posted(PR, "fix_exhausted", token)

    assert q.release_pr_announcement(PR, "fix_exhausted", token) is False

    assert q.pr_announcement_state(PR, "fix_exhausted")["posted_at"] is not None
    assert q.claim_pr_announcement(PR, "fix_exhausted") is None


def test_a_claim_abandoned_mid_post_is_reclaimable(tmp_db):
    """⛔A process killed between claiming and posting must not silence the
    notice for good. An unposted claim ages out; a posted one never does."""
    q = TaskQueue(tmp_db)
    first = q.claim_pr_announcement(PR, "fix_exhausted", claimed_by="dead-task")
    assert first

    second = q.claim_pr_announcement(PR, "fix_exhausted", stale_after=0.0)
    assert second and second != first
    assert q.pr_announcement_state(PR, "fix_exhausted")["claimed_by"] == ""

    q.mark_pr_announcement_posted(PR, "fix_exhausted", second)
    assert q.claim_pr_announcement(PR, "fix_exhausted", stale_after=0.0) is None


def test_distinct_prs_do_not_share_a_claim(tmp_db):
    q = TaskQueue(tmp_db)
    assert q.claim_pr_announcement(251, "fix_exhausted")
    assert q.claim_pr_announcement(252, "fix_exhausted")
    assert q.claim_pr_announcement(251, "fix_exhausted") is None


def test_a_broken_prior_notice_check_still_posts(tmp_db, monkeypatch):
    """The best-effort GitHub scan may fail; the escalation may not be lost
    because of it. Same rule as `None`: when we cannot tell, we post."""
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "3")
    q = TaskQueue(tmp_db)
    posted = []

    def boom(pr, marker):
        raise RuntimeError("gh pr view exploded")

    auto_enqueue_fix(q, _exhausted_review(q), pr_state_fn=_state("open"),
                     already_announced_fn=boom,
                     comment_fn=lambda pr, body: posted.append(body))

    assert len(posted) == 1



# ── 9. the lease needs a fence, not just an expiry ────────────────────
#
# Taking a stale claim over does not stop the previous owner from still
# running. If A is merely SLOW rather than dead, B reclaims, and then A — which
# knows nothing about B — marks B's claim posted, or releases it. Both put a
# second notice on the PR, which is the defect the claim exists to prevent.
# Reproduced before fixing; the earlier test only modelled a DEAD owner, which
# is the case that happens to be safe (review of PR #251, round 3).


def test_a_superseded_owner_cannot_mark_the_new_owners_claim_posted(tmp_db):
    """★Scenario 1: slow A finishes after B took over."""
    q = TaskQueue(tmp_db)
    a = q.claim_pr_announcement(PR, "fix_exhausted", claimed_by="A")
    b = q.claim_pr_announcement(PR, "fix_exhausted", claimed_by="B", stale_after=0.0)
    assert a and b and a != b

    assert q.mark_pr_announcement_posted(PR, "fix_exhausted", a) is False

    state = q.pr_announcement_state(PR, "fix_exhausted")
    assert state["claimed_by"] == "B" and state["posted_at"] is None, \
        "the superseded owner silenced the live one"
    assert q.owns_pr_announcement(PR, "fix_exhausted", b) is True


def test_a_superseded_owner_cannot_delete_the_new_owners_claim(tmp_db):
    """★Scenario 2: A's post fails after B took over. An unconditional release
    would hand a third worker the right to post alongside B."""
    q = TaskQueue(tmp_db)
    a = q.claim_pr_announcement(PR, "fix_exhausted", claimed_by="A")
    b = q.claim_pr_announcement(PR, "fix_exhausted", claimed_by="B", stale_after=0.0)

    assert q.release_pr_announcement(PR, "fix_exhausted", a) is False

    assert q.pr_announcement_state(PR, "fix_exhausted")["claimed_by"] == "B"
    assert q.claim_pr_announcement(PR, "fix_exhausted", claimed_by="C") is None, \
        "a third worker could claim while B still holds it"
    assert q.owns_pr_announcement(PR, "fix_exhausted", b) is True


def test_a_worker_that_lost_the_lease_does_not_post(tmp_db, monkeypatch):
    """⛔Ownership is validated immediately before the side effect. A comment
    cannot be un-posted, so losing the claim must prevent the post, not merely
    be discovered afterwards."""
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "3")
    q = TaskQueue(tmp_db)
    posted = []

    def steal_then_report(pr, marker):
        # Between claiming and posting, another worker takes the stale lease.
        q.claim_pr_announcement(PR, "fix_exhausted", claimed_by="B", stale_after=0.0)
        return False

    auto_enqueue_fix(q, _exhausted_review(q), pr_state_fn=_state("open"),
                     already_announced_fn=steal_then_report,
                     comment_fn=lambda pr, body: posted.append(body))

    assert posted == [], "posted after the lease was taken over"
    assert q.pr_announcement_state(PR, "fix_exhausted")["claimed_by"] == "B"


def test_the_token_is_opaque_and_unguessable(tmp_db):
    """The fence must not be derivable from the row — otherwise a superseded
    worker could reconstruct it."""
    q = TaskQueue(tmp_db)
    token = q.claim_pr_announcement(PR, "fix_exhausted", claimed_by="review-abc")

    assert isinstance(token, str) and len(token) >= 16
    assert "review-abc" not in token and str(PR) not in token
    assert q.owns_pr_announcement(PR, "fix_exhausted", "") is False
    assert q.owns_pr_announcement(PR, "fix_exhausted", "not-the-token") is False
