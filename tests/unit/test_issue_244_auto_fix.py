"""#244 — `review request_changes` must cascade into a fix task.

The stage cascade automated `implement completed → review` and
`review approve → test`, but the rejection path just ended. Every additional
review round therefore needed an operator to hand-enqueue an implement task
with the reviewer's findings pasted into the description — `crew triage
--watch` automated the first claim and nothing after it.

The transition is only safe with a bound, so most of these tests are about the
bound and the refusals:

  * a reviewer that keeps rejecting must not drive review→fix forever;
  * the round counter must survive the round trip through a NEW review task,
    or the cap is decorative;
  * a review that crashed is not a fix request, however `_resolve_verdict`
    reports it;
  * `crew run`'s own loop must not be double-driven.
"""

import uuid

import pytest

from agent_crew.pipeline import (
    DEFAULT_REVIEW_FIX_MAX_ROUNDS,
    auto_enqueue_fix,
    auto_enqueue_review,
    review_fix_max_rounds,
)
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue

BRANCH = "agent/claude/244-x"
FINDING = "HIGH src/agent_crew/watch.py:759 - the cap drops the AC"


@pytest.fixture
def q(tmp_db):
    return TaskQueue(tmp_db)


def _review(q, *, findings=(FINDING,), summary="request_changes: fix the cap",
            verdict="request_changes", status="completed", pr_number=None,
            context=None, project="", branch=BRANCH):
    review_id = f"review-{uuid.uuid4().hex[:8]}"
    ctx = {"prev_task_id": "impl-1"}
    if pr_number is not None:
        ctx["pr_number"] = pr_number
    ctx.update(context or {})
    q.enqueue(TaskRequest(task_id=review_id, task_type="review",
                          description="review", branch=branch, context=ctx,
                          project=project))
    q.submit_result(review_id, TaskResult(
        task_id=review_id, status=status, summary=summary, verdict=verdict,
        findings=list(findings), pr_number=pr_number))
    return review_id


def _task(q, task_id):
    return {t.task_id: t for t in q.list_tasks()}[task_id]


# ── 1. the transition itself ──────────────────────────────────────────


def test_request_changes_enqueues_a_fix_task(q):
    """★The whole point of #244 — a rejection produces queued work."""
    review_id = _review(q, pr_number=241)

    fix_id = auto_enqueue_fix(q, review_id)

    assert fix_id is not None
    fix = _task(q, fix_id)
    assert fix.task_type == "implement"
    assert fix.branch == BRANCH, "a fix must land on the branch under review"
    assert fix.context["prev_task_id"] == review_id


def test_the_findings_are_in_the_description(q):
    """The content a human would otherwise have copy-pasted."""
    review_id = _review(q, findings=[FINDING, "MED: no test for the boundary"],
                        summary="two things", pr_number=241)

    fix = _task(q, auto_enqueue_fix(q, review_id))

    assert FINDING in fix.description
    assert "no test for the boundary" in fix.description
    assert "two things" in fix.description
    assert "PR #241" in fix.description
    # ...and the branch instruction, so a fix round cannot fork a second PR.
    assert "SAME branch" in fix.description


def test_findings_also_ride_in_the_context(q):
    """Structured, not only prose — the next consumer should not have to
    re-parse a description to know what was raised."""
    review_id = _review(q, findings=[FINDING])

    fix = _task(q, auto_enqueue_fix(q, review_id))

    assert fix.context["review_findings"] == [FINDING]


def test_pr_number_is_propagated_for_worktree_checkout(q):
    """#186: `pr_number` in context is what lets the dispatcher check out the
    PR head for this task."""
    review_id = _review(q, pr_number=241)

    fix = _task(q, auto_enqueue_fix(q, review_id))

    assert fix.context["pr_number"] == 241


def test_no_tester_and_issue_identity_survive_the_round_trip(q):
    review_id = _review(q, context={"no_tester": True, "issue": 244,
                                    "issue_title": "auto-fix", "repo": "org/repo"})

    fix = _task(q, auto_enqueue_fix(q, review_id))

    assert fix.context["no_tester"] is True
    assert fix.context["issue"] == 244
    assert fix.context["repo"] == "org/repo"


def test_project_is_inherited(q):
    review_id = _review(q, project="agent_crew")

    assert _task(q, auto_enqueue_fix(q, review_id)).project == "agent_crew"


# ── 2. the refusals ───────────────────────────────────────────────────


def test_an_approved_review_enqueues_no_fix(q):
    review_id = _review(q, verdict="approve", findings=[], summary="lgtm")

    assert auto_enqueue_fix(q, review_id) is None


def test_a_crashed_review_is_not_a_fix_request(q):
    """⛔`_resolve_verdict` maps a failed review to `request_changes` so a
    broken review can never silently approve (#100). That protection must not
    turn a reviewer crash into implementation work: there are no findings, and
    the failure path already retries or falls back — a fix task here would
    double-handle it AND hand an agent nothing to do."""
    review_id = _review(q, status="failed", verdict=None, findings=[],
                        summary="dispatcher timeout")

    assert auto_enqueue_fix(q, review_id) is None
    assert not [t for t in q.list_tasks() if t.task_type == "implement"]


def test_request_changes_with_nothing_actionable_enqueues_nothing(q):
    """A fix task whose description says only "changes were requested" is
    worse than no task — the agent has nothing to reproduce."""
    review_id = _review(q, verdict="request_changes", findings=[], summary="")

    assert auto_enqueue_fix(q, review_id) is None


def test_coordinator_managed_review_is_skipped(q):
    """`crew run` drives its own loop; a second enqueue here races it."""
    review_id = _review(q, context={"coordinator_managed": True})

    assert auto_enqueue_fix(q, review_id) is None


def test_cross_project_review_is_skipped(q):
    review_id = _review(q, project="other_project")

    assert auto_enqueue_fix(q, review_id, server_project="agent_crew") is None


def test_a_missing_review_task_is_not_an_error(q):
    assert auto_enqueue_fix(q, "review-does-not-exist") is None


def test_a_review_with_no_result_yet_enqueues_nothing(q):
    review_id = f"review-{uuid.uuid4().hex[:8]}"
    q.enqueue(TaskRequest(task_id=review_id, task_type="review",
                          description="review", branch=BRANCH))

    assert auto_enqueue_fix(q, review_id) is None


# ── 3. the round cap ──────────────────────────────────────────────────


def test_round_counter_starts_at_one_and_is_recorded(q):
    fix = _task(q, auto_enqueue_fix(q, _review(q)))

    assert fix.context["fix_round"] == 1
    assert f"round 1/{DEFAULT_REVIEW_FIX_MAX_ROUNDS}" in fix.description


def test_the_round_past_the_cap_does_not_enqueue(q, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "3")
    at_cap = _review(q, context={"fix_round": 3})

    assert auto_enqueue_fix(q, at_cap) is None
    assert not [t for t in q.list_tasks() if t.task_type == "implement"]


def test_the_last_allowed_round_still_enqueues(q, monkeypatch):
    """The cap is a limit, not an off-by-one."""
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "3")

    fix = _task(q, auto_enqueue_fix(q, _review(q, context={"fix_round": 2})))

    assert fix.context["fix_round"] == 3


def test_exhausting_the_budget_says_so_on_the_pr(q, monkeypatch):
    """⛔A silent stop is the worst outcome: the PR would go quiet after a
    rejection and look like it was still being worked on."""
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "2")
    posted = []
    review_id = _review(q, pr_number=241, context={"fix_round": 2})

    auto_enqueue_fix(q, review_id,
                     comment_fn=lambda pr, body: posted.append((pr, body)))

    assert len(posted) == 1
    pr, body = posted[0]
    assert pr == 241
    assert "exhausted" in body.lower()
    assert FINDING in body, "the outstanding findings must survive the handoff"
    assert "human" in body.lower()


def test_a_failure_to_comment_does_not_raise(q, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "1")

    def boom(pr, body):
        raise RuntimeError("gh is down")

    assert auto_enqueue_fix(q, _review(q, pr_number=241,
                                       context={"fix_round": 1}),
                            comment_fn=boom) is None


def test_zero_rounds_disables_the_transition(q, monkeypatch):
    """The kill switch: an operator who does not want this automation at all."""
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "0")

    assert auto_enqueue_fix(q, _review(q)) is None


def test_the_cap_is_read_at_call_time_and_survives_garbage(monkeypatch):
    monkeypatch.delenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", raising=False)
    assert review_fix_max_rounds() == DEFAULT_REVIEW_FIX_MAX_ROUNDS
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "5")
    assert review_fix_max_rounds() == 5
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "not-a-number")
    assert review_fix_max_rounds() == DEFAULT_REVIEW_FIX_MAX_ROUNDS
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "-2")
    assert review_fix_max_rounds() == 0


# ── 4. the cap must bound the REAL loop, not one call ─────────────────


def test_the_counter_survives_the_round_trip_through_a_new_review(q):
    """★★The test that makes the cap real.

    Each round mints new task ids, so the counter has to ride the lineage:
    review → fix → (auto) review → fix. If `auto_enqueue_review` drops
    `fix_round`, every round reads zero, the cap is never reached, and a
    stubborn reviewer loops forever. Driving the actual cascade functions is
    the only way to catch that — a single `auto_enqueue_fix` call cannot.
    """
    review_id = _review(q, pr_number=241)
    rounds = []

    for _ in range(6):                      # more attempts than the cap allows
        fix_id = auto_enqueue_fix(q, review_id)
        if fix_id is None:
            break
        rounds.append(_task(q, fix_id).context["fix_round"])
        # the fix completes → the cascade raises a fresh review task
        q.submit_result(fix_id, TaskResult(
            task_id=fix_id, status="completed", summary="fixed", pr_number=241))
        review_id = auto_enqueue_review(q, fix_id, 241)
        assert review_id is not None
        q.submit_result(review_id, TaskResult(
            task_id=review_id, status="completed", summary="still no",
            verdict="request_changes", findings=[FINDING], pr_number=241))

    assert rounds == [1, 2, 3], \
        f"the loop ran {len(rounds)} automated rounds against a cap of 3"
    assert len([t for t in q.list_tasks() if t.task_type == "implement"]) == 3


# ── 5. bounded, but never silently ────────────────────────────────────


def test_a_long_finding_is_truncated_visibly(q):
    review_id = _review(q, findings=["X" * 5000])

    desc = _task(q, auto_enqueue_fix(q, review_id)).description

    assert "truncated" in desc
    assert review_id in desc, "it must say where the full text lives"


def test_excess_findings_are_counted_not_dropped(q):
    review_id = _review(q, findings=[f"finding {i}" for i in range(30)])

    fix = _task(q, auto_enqueue_fix(q, review_id))

    assert "finding 0" in fix.description
    assert "10 further findings omitted" in fix.description
    # ...and nothing is lost: the full list is still in the context.
    assert len(fix.context["review_findings"]) == 30


# ── 6. the HTTP result handler (where the branch has to actually fire) ─


def _server(tmp_db, push, **kw):
    from agent_crew.server import create_app

    return create_app(db_path=tmp_db,
                      pane_map={"implementer": "%1", "reviewer": "%2",
                                "tester": "%3"},
                      port=8100, push_fn=push, **kw)


class _RecordingPush:
    def __init__(self):
        self.calls = []

    def __call__(self, pane_id, text):
        self.calls.append((pane_id, text))


def _post_review_result(client, review_id, **kw):
    payload = {"task_id": review_id, "status": "completed",
               "summary": "request_changes: the cap drops the AC",
               "verdict": "request_changes", "findings": [FINDING],
               "pr_number": None}
    payload.update(kw)
    return client.post(f"/tasks/{review_id}/result", json=payload)


def _enqueue_review(client, review_id, context=None):
    return client.post("/tasks", json={
        "task_id": review_id, "task_type": "review", "description": "review",
        "branch": BRANCH, "priority": 3, "context": context or {},
        "project": ""})


def test_http_request_changes_result_enqueues_and_pushes_a_fix(tmp_db):
    """★★End to end through the real handler — the layer #244 reported.

    The cascade functions could be perfect and the feature still absent if the
    handler never calls them; that is exactly what the issue found.
    """
    from fastapi.testclient import TestClient

    push = _RecordingPush()
    with TestClient(_server(tmp_db, push)) as client:
        _enqueue_review(client, "review-http-1")
        assert _post_review_result(client, "review-http-1").status_code == 200

    fixes = [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "implement"]
    assert len(fixes) == 1
    assert FINDING in fixes[0].description
    # ...and it was pushed to the implementer pane, not left sitting in the queue.
    assert any(pane == "%1" and fixes[0].task_id in text
               for pane, text in push.calls)


def test_http_coordinator_managed_review_enqueues_no_fix(tmp_db):
    from fastapi.testclient import TestClient

    push = _RecordingPush()
    with TestClient(_server(tmp_db, push)) as client:
        _enqueue_review(client, "review-http-2", {"coordinator_managed": True})
        assert _post_review_result(client, "review-http-2").status_code == 200

    assert not [t for t in TaskQueue(tmp_db).list_tasks()
                if t.task_type == "implement"]


def test_http_approved_review_still_goes_to_test_not_fix(tmp_db):
    """The new branch must not shadow the approve path."""
    from fastapi.testclient import TestClient

    push = _RecordingPush()
    with TestClient(_server(tmp_db, push)) as client:
        _enqueue_review(client, "review-http-3")
        assert _post_review_result(client, "review-http-3", verdict="approve",
                                   findings=[], summary="lgtm").status_code == 200

    types = [t.task_type for t in TaskQueue(tmp_db).list_tasks()]
    assert "test" in types
    assert "implement" not in types


def test_http_result_submission_survives_a_broken_cascade(tmp_db, monkeypatch):
    """⛔Auto-enqueue must never fail a result submission — an agent that
    cannot POST its result is a task the dispatcher will later mark failed."""
    from fastapi.testclient import TestClient

    def boom(*a, **k):
        raise RuntimeError("queue exploded")

    monkeypatch.setattr("agent_crew.server._pipeline_auto_enqueue_fix", boom)
    push = _RecordingPush()
    with TestClient(_server(tmp_db, push)) as client:
        _enqueue_review(client, "review-http-4")
        assert _post_review_result(client, "review-http-4").status_code == 200


# ── 7. idempotency: a replayed result must not fork the work ──────────
#
# `submit_result` has no "already done" guard, so a retried or duplicated
# result POST re-runs the whole cascade. With a random fix task id every
# replay minted a NEW task: two implementers, same branch, same findings,
# concurrently — and both recorded as round 1, so the round cap could not
# even see them as separate rounds (review of PR #245).


def test_a_replayed_review_result_does_not_fork_a_second_fix(q):
    """★The regression."""
    review_id = _review(q, pr_number=245)

    first = auto_enqueue_fix(q, review_id)
    second = auto_enqueue_fix(q, review_id)

    assert first is not None
    assert second is None, "a replay must not create a second fix task"
    assert [t.task_id for t in q.list_tasks() if t.task_type == "implement"] == [first]


def test_the_fix_id_is_derived_from_the_review_round(q):
    """The id IS the guard, so it has to be a pure function of the round."""
    from agent_crew.pipeline import fix_task_id

    assert fix_task_id("review-abc", 1) == fix_task_id("review-abc", 1)
    assert fix_task_id("review-abc", 1) != fix_task_id("review-abc", 2)
    assert fix_task_id("review-abc", 1) != fix_task_id("review-def", 1)

    review_id = _review(q)
    assert auto_enqueue_fix(q, review_id) == fix_task_id(review_id, 1)


def test_two_reviews_never_share_a_key(q):
    """★A key collision drops a fix instead of duplicating one.

    The first version of this key was `sha256(review_task_id)[:8]` — 32 bits,
    which collide by birthday at roughly 65k reviews. These two ids are a real
    colliding pair for that scheme. Sharing a key means the second review's
    `request_changes` is silently swallowed as "already exists": no fix task,
    no error, nothing to notice. That is strictly worse than the duplicate the
    key exists to prevent, because a duplicate is at least visible.
    """
    from agent_crew.pipeline import fix_task_id

    a, b = "review-000252f9", "review-00034f12"
    assert fix_task_id(a, 1) != fix_task_id(b, 1)

    first = _review(q, context={}, findings=["finding for A"])
    second = _review(q, context={}, findings=["finding for B"])
    # ...and through the real path, with those exact ids:
    for rid in (a, b):
        q.enqueue(TaskRequest(task_id=rid, task_type="review", description="r",
                              branch=BRANCH, context={}))
        q.submit_result(rid, TaskResult(
            task_id=rid, status="completed", summary="fix it",
            verdict="request_changes", findings=[f"finding for {rid}"]))

    assert auto_enqueue_fix(q, a) is not None
    assert auto_enqueue_fix(q, b) is not None, \
        "the second review's fix was swallowed by a key collision"
    assert first and second      # the fixture reviews are untouched by this


def test_the_key_is_injective_not_merely_improbable():
    """⛔Exhaustive over the shapes that could alias, not a spot check.

    `-r<digits>` has to be unambiguous from the right: a review id that itself
    ends in `-r2` must not collide with a different review at another round.
    """
    from agent_crew.pipeline import fix_task_id

    review_ids = ["review-abc", "review-abc-r2", "review-abc-r", "review-ab",
                  "review-abc-r22", "r", "", "review-abc-",
                  # ids ending in digits: the shape that aliases if the round
                  # is appended without a delimiter.
                  "review-abc1", "review-abc12", "review-abc1-r1", "review-1"]
    pairs = [(rid, n) for rid in review_ids for n in range(1, 13)]
    keys = [fix_task_id(rid, n) for rid, n in pairs]

    assert len(set(keys)) == len(pairs), "two distinct (review, round) pairs share a key"


def test_the_key_names_the_review_it_belongs_to(q):
    """An operator reading the queue should not need a lookup to tell which
    review a fix task came from."""
    review_id = _review(q)

    fix_id = auto_enqueue_fix(q, review_id)

    assert review_id in fix_id and fix_id.endswith("-r1")


def test_idempotency_holds_however_far_the_fix_has_progressed(q):
    """Keyed on the round, not on the task's state.

    A completed fix followed by a replayed POST must not re-open the work, and
    an operator's explicit cancel must not be quietly undone by a duplicate
    delivery either.
    """
    review_id = _review(q)
    fix_id = auto_enqueue_fix(q, review_id)
    q.submit_result(fix_id, TaskResult(task_id=fix_id, status="completed",
                                       summary="fixed"))

    assert auto_enqueue_fix(q, review_id) is None
    assert len([t for t in q.list_tasks() if t.task_type == "implement"]) == 1


def test_distinct_reviews_still_get_their_own_fix(q):
    """⛔Idempotency must not collapse two genuinely different rejections."""
    first = auto_enqueue_fix(q, _review(q))
    second = auto_enqueue_fix(q, _review(q))

    assert first is not None and second is not None and first != second
    assert len([t for t in q.list_tasks() if t.task_type == "implement"]) == 2


def test_concurrent_submissions_produce_exactly_one_fix(q, tmp_db):
    """★★The race a check-then-act cannot close.

    Two threads both read "no fix exists" and both insert. The guard is the
    `tasks` PRIMARY KEY, so the loser's INSERT fails and it reports creating
    nothing — same shape as #224, where the label could not be the mutex.
    """
    import threading

    review_id = _review(q, pr_number=245)
    start = threading.Barrier(4)
    results = []
    lock = threading.Lock()

    def _submit():
        own_queue = TaskQueue(tmp_db)      # a separate connection, as in prod
        start.wait()
        got = auto_enqueue_fix(own_queue, review_id)
        with lock:
            results.append(got)

    threads = [threading.Thread(target=_submit) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    created = [r for r in results if r]
    assert len(created) == 1, f"{len(created)} threads each believed they created a fix"
    assert len([t for t in q.list_tasks() if t.task_type == "implement"]) == 1


def test_http_duplicate_result_post_enqueues_one_fix(tmp_db):
    """★★End to end: the same POST twice, as the reporter described it.

    `submit_result` has no already-done guard, so the second POST really does
    re-run the cascade — the idempotency has to live below it.
    """
    from fastapi.testclient import TestClient

    push = _RecordingPush()
    with TestClient(_server(tmp_db, push)) as client:
        _enqueue_review(client, "review-http-dup")
        assert _post_review_result(client, "review-http-dup").status_code == 200
        assert _post_review_result(client, "review-http-dup").status_code == 200

    fixes = [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "implement"]
    assert len(fixes) == 1, f"duplicate POST produced {len(fixes)} fix tasks"
