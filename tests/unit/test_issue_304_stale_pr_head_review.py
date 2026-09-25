"""#304 — a verdict must not be attributed to a commit that is no longer the head.

Reported from a downstream repo, 2026-09-14. A review was dispatched with its
worktree pinned at `5b496a9f`, the PR head was `28419e96` by the time the review
ran, and a `request_changes` verdict was published anyway. The head has since
moved again to `d29a1052`, and no head-anchored review exists — so review
capacity was spent producing a blocker about code nobody is looking at.

What already existed, and why it was not enough:

- #286 pins the review to the commit it was prepared at and keeps that pin
  immutable, so preparing twice cannot silently move the review. That is
  deliberate and stays: re-resolving the pin at dispatch would make
  `reviewed_sha` a lie about what was actually read.
- #253's `review_is_current` compares the pin against the live head — but only
  to decide whether to create a FIX TASK. The verdict itself was published to
  the PR before that gate was ever consulted, so the stale review still landed
  as a blocker and only the follow-up work was suppressed.

The missing invariant is the publication one: *if the head moved, the verdict is
about a different commit and must not be posted as though it were about this
one.* Rather than a second comparison living next to the first, the three-state
primitive below is the single answer to "where is this review relative to the
head", and `review_is_current` is rebuilt on top of it — two copies of that
question would drift, and the drift would be invisible until it published a
wrong verdict.
"""

import pytest

from agent_crew.pipeline import (
    review_head_status,
    review_is_current,
    review_publication_decision,
    stale_review_task_id,
)

PINNED = "5b496a9f8143f4a9484aeb353152cf52e7a90bcd"
MOVED_TO = "28419e9626f06713552225be3abe38d3ee2835a1"
MOVED_AGAIN = "d29a105211acbc83802976a9928c7a2fdf8e713b"


def _ctx(sha=PINNED, **over):
    ctx = {"reviewed_sha": sha, "repo": "owner/repo", "pr_number": 5652}
    ctx.update(over)
    return ctx


def _head(value):
    return lambda _pr: value


# ── 1. the three states ───────────────────────────────────────────────


def test_the_head_still_being_the_reviewed_commit_is_current():
    status, head, _ = review_head_status(_ctx(), 5652, head_sha_fn=_head(PINNED))
    assert status == "current"
    assert head == PINNED


def test_the_head_having_moved_is_stale():
    """★★The reported incident, as data."""
    status, head, why = review_head_status(_ctx(), 5652, head_sha_fn=_head(MOVED_TO))
    assert status == "stale"
    assert head == MOVED_TO, "the new head must be reported, not just the fact of a move"
    assert PINNED[:9] in why and MOVED_TO[:9] in why


@pytest.mark.parametrize("head_value", ["", None])
def test_a_head_that_cannot_be_read_is_unknown_not_current(head_value):
    """⛔Fail closed. `unknown` is its own state precisely so it cannot be
    mistaken for `current` — publishing a verdict because a lookup failed is
    the same error as publishing one against a moved head."""
    status, head, _ = review_head_status(_ctx(), 5652, head_sha_fn=_head(head_value))
    assert status == "unknown"
    assert head == ""


def test_a_lookup_that_raises_is_unknown():
    def _boom(_pr):
        raise RuntimeError("gh exploded")

    assert review_head_status(_ctx(), 5652, head_sha_fn=_boom)[0] == "unknown"


def test_a_review_with_no_pin_is_unpinned():
    """Older tasks and producers that never went through worktree prep record
    no pin. That is not staleness and must not be reported as such."""
    assert review_head_status(_ctx(sha=""), 5652, head_sha_fn=_head(MOVED_TO))[0] \
        == "unpinned"


def test_a_review_with_no_pr_is_unpinned():
    assert review_head_status(_ctx(), None, head_sha_fn=_head(MOVED_TO))[0] == "unpinned"


# ── 2. the existing gate is rebuilt on it, not duplicated ─────────────


@pytest.mark.parametrize("head,expected", [
    (PINNED, True),
    (MOVED_TO, False),
    ("", False),
])
def test_review_is_current_still_answers_exactly_as_before(head, expected):
    """⛔#253's contract is unchanged: unpinned and current pass, stale and
    unknown defer. Rebuilding it on the new primitive must not move that line —
    the fix cascade's behaviour is not what this issue is about."""
    assert review_is_current(_ctx(), 5652, head_sha_fn=_head(head))[0] is expected


def test_an_unpinned_review_still_passes_the_cascade_gate():
    assert review_is_current(_ctx(sha=""), 5652, head_sha_fn=_head(MOVED_TO))[0] is True


def test_one_primitive_answers_for_both_call_sites():
    """Two copies of "has the head moved" would drift, and the drift would be
    invisible until one of them published a verdict the other would have
    stopped."""
    import inspect

    from agent_crew import pipeline

    source = inspect.getsource(pipeline.review_is_current)
    assert "review_head_status(" in source, \
        "review_is_current reimplements the comparison instead of reusing it"


# ── 3. the publication decision ───────────────────────────────────────


def test_a_current_review_is_published():
    decision = review_publication_decision(_ctx(), 5652, head_sha_fn=_head(PINNED))
    assert decision.publish is True
    assert decision.requeue_head == ""


def test_a_stale_review_is_not_published():
    """★★The invariant this issue exists for. The verdict describes `5b496a9f`;
    posting it against `28419e96` attributes a judgement to code that was never
    read."""
    decision = review_publication_decision(_ctx(), 5652, head_sha_fn=_head(MOVED_TO))
    assert decision.publish is False
    assert decision.status == "stale"


def test_a_stale_review_requeues_against_the_new_head():
    """Not merely suppressed: the PR still needs a review, and the head it needs
    one against is known right here."""
    decision = review_publication_decision(_ctx(), 5652, head_sha_fn=_head(MOVED_TO))
    assert decision.requeue_head == MOVED_TO


def test_an_unknown_head_is_not_published_and_not_requeued():
    """⛔Asymmetric on purpose. Not publishing is recoverable — the review result
    is still recorded and a human or a later head-anchored round can act on it.
    Requeueing on a head we failed to READ would spend a reviewer against a
    commit we cannot name, so unknown stops rather than guesses."""
    decision = review_publication_decision(_ctx(), 5652, head_sha_fn=_head(""))
    assert decision.publish is False
    assert decision.requeue_head == ""


def test_an_unpinned_review_is_still_published():
    """⛔The compatibility control. Every review that predates pinning, and
    every producer that does not pin, must keep working — refusing them all
    would disable review publication to fix a subset."""
    decision = review_publication_decision(_ctx(sha=""), 5652, head_sha_fn=_head(MOVED_TO))
    assert decision.publish is True


def test_a_review_with_no_pr_is_published():
    """Nothing to be stale against."""
    assert review_publication_decision(_ctx(), None, head_sha_fn=_head(MOVED_TO)).publish


def test_the_decision_says_why():
    """A suppressed verdict that does not explain itself is indistinguishable
    from a lost one."""
    decision = review_publication_decision(_ctx(), 5652, head_sha_fn=_head(MOVED_TO))
    assert PINNED[:9] in decision.reason and MOVED_TO[:9] in decision.reason


# ── 4. the requeue cannot multiply ────────────────────────────────────


def test_the_requeue_task_id_is_derived_from_the_head():
    """⛔Deterministic, like #244's fix task id, and for the same reason: a
    result POST can arrive twice, and two random ids would mean two reviewers
    dispatched against one commit."""
    assert stale_review_task_id(5652, MOVED_TO) == stale_review_task_id(5652, MOVED_TO)


def test_a_different_head_gets_a_different_requeue():
    """The head moving again is a genuinely new review, not a duplicate."""
    assert stale_review_task_id(5652, MOVED_TO) != stale_review_task_id(5652, MOVED_AGAIN)


def test_a_different_pr_gets_a_different_requeue():
    assert stale_review_task_id(5652, MOVED_TO) != stale_review_task_id(5653, MOVED_TO)


def test_the_requeue_id_is_a_usable_task_id():
    task_id = stale_review_task_id(5652, MOVED_TO)
    assert task_id.startswith("review-")
    assert len(task_id) <= 64 and " " not in task_id
    assert MOVED_TO[:12] in task_id, \
        "the head must be readable in the id — an opaque hash cannot be traced back"


# ── 5. the server actually consults it ────────────────────────────────


def test_the_publish_path_is_gated():
    """⛔The decision is inert unless the publish site asks it. `post_review_comment`
    was called unconditionally for any review carrying a pr_number; a pure
    function nobody calls would leave the incident exactly as reported."""
    import inspect

    from agent_crew import server

    source = inspect.getsource(server)
    assert "review_publication_decision(" in source, \
        "the verdict is still published without consulting the head"


# ── 6. end to end, through the real result path ───────────────────────


def _server(tmp_db, *, unused_tcp_port):
    from agent_crew.server import create_app

    return create_app(db_path=tmp_db, pane_map={}, port=unused_tcp_port,
                      watchdog_disabled=True, anomaly_disabled=True,
                      push_fn=lambda *a, **k: None)


def _submit_review(tmp_db, monkeypatch, head, *, unused_tcp_port):
    """Post a review result whose pin is PINNED, with the live head stubbed."""
    from fastapi.testclient import TestClient

    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue

    posted = []
    monkeypatch.setattr("agent_crew.github.post_review_comment",
                        lambda **kw: posted.append(kw))
    monkeypatch.setattr("agent_crew.github.pr_head_sha",
                        lambda *a, **k: head)

    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(
        task_id="review-stale", task_type="review", description="Review PR #5652",
        branch="main", context={"pr_number": 5652, "repo": "owner/repo",
                                "reviewed_sha": PINNED, "coordinator_managed": True}))
    with TestClient(_server(tmp_db, unused_tcp_port=unused_tcp_port)) as client:
        response = client.post("/tasks/review-stale/result", json={
            "task_id": "review-stale", "status": "completed",
            "summary": "blocker", "verdict": "request_changes",
            "findings": ["code_quality: x"],
            "pr_number": 5652})
        assert response.status_code == 200, response.text
    return posted, TaskQueue(tmp_db)


def test_a_stale_verdict_is_not_posted_to_the_pr(tmp_db, monkeypatch, *, unused_tcp_port):
    """★★The incident, through the real POST path. A `request_changes` verdict
    about `5b496a9f` must not land on a PR whose head is `28419e96`."""
    posted, _ = _submit_review(tmp_db, monkeypatch, MOVED_TO, unused_tcp_port=unused_tcp_port)
    assert posted == [], f"published a verdict against a moved head: {posted}"


def test_a_current_verdict_is_still_posted(tmp_db, monkeypatch, *, unused_tcp_port):
    """⛔The control. Suppressing every verdict would 'fix' this by disabling
    review publication entirely."""
    posted, _ = _submit_review(tmp_db, monkeypatch, PINNED, unused_tcp_port=unused_tcp_port)
    assert len(posted) == 1
    assert posted[0]["pr_number"] == 5652


def test_the_suppression_is_recorded_on_the_task(tmp_db, monkeypatch, *, unused_tcp_port):
    """A verdict that vanishes without a trace is indistinguishable from one
    that was lost."""
    _, q = _submit_review(tmp_db, monkeypatch, MOVED_TO, unused_tcp_port=unused_tcp_port)
    ctx = q.get_task_context("review-stale")
    assert ctx.get("review_publication") == "stale"
    assert MOVED_TO[:9] in (ctx.get("review_publication_reason") or "")


def test_a_stale_review_requeues_exactly_one_head_anchored_review(tmp_db, monkeypatch, *, unused_tcp_port):
    """★★The other half: the PR still needs a review at the head it actually
    has. Exactly one — the id is derived, so a duplicate result cannot put two
    reviewers on one commit."""
    _, q = _submit_review(tmp_db, monkeypatch, MOVED_TO, unused_tcp_port=unused_tcp_port)
    requeued = [t for t in q.list_tasks() if t.task_id == stale_review_task_id(5652, MOVED_TO)]
    assert len(requeued) == 1
    assert requeued[0].task_type == "review"
    assert requeued[0].context.get("expected_head_sha") == MOVED_TO
    assert requeued[0].context.get("superseded_review") == "review-stale"


def test_the_result_itself_is_still_recorded(tmp_db, monkeypatch, *, unused_tcp_port):
    """⛔Only the attribution is stopped, never the audit trail — the standing
    rule for every gate in this pipeline."""
    _, q = _submit_review(tmp_db, monkeypatch, MOVED_TO, unused_tcp_port=unused_tcp_port)
    assert q.get_result("review-stale") is not None


# ── 7. review of PR #306: suppressing the COMMENT was not enough ──────
#
# P1. #304 gated `post_review_comment` and stopped there, so the verdict's
# CONSEQUENCES still ran: an approve with `no_tester=True` reached
# `_auto_merge_pr`, and otherwise it enqueued a tester. A stale approval of the
# old commit therefore merged a PR whose head had moved — strictly worse than
# the mis-attributed comment #304 set out to prevent, because a merge cannot be
# taken back by a later head-anchored review.


def _submit_approve(tmp_db, monkeypatch, head, *, no_tester=False, unused_tcp_port):
    """Post an APPROVING review whose pin is PINNED, with the live head stubbed."""
    from fastapi.testclient import TestClient

    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue
    from agent_crew.server import create_app

    merged, tests, posted = [], [], []
    monkeypatch.setattr("agent_crew.github.post_review_comment",
                        lambda **kw: posted.append(kw))
    monkeypatch.setattr("agent_crew.github.pr_head_sha", lambda *a, **k: head)
    # ⛔Without an open PR state the terminal-PR gate skips the test enqueue for
    #   EVERY case, so "no tester was enqueued" would pass for a stale review
    #   and for a current one alike — the control would prove nothing.
    monkeypatch.setattr("agent_crew.github.pr_state", lambda *a, **k: "open")
    monkeypatch.setattr("agent_crew.github.get_repo", lambda *a, **k: "owner/repo")
    monkeypatch.setattr("agent_crew.github.merge_pr",
                        lambda n, **k: merged.append(n) or True)

    ctx = {"pr_number": 5652, "repo": "owner/repo", "reviewed_sha": PINNED}
    if no_tester:
        ctx["no_tester"] = True
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="review-approve", task_type="review",
                          description="Review PR #5652", branch="main", context=ctx))

    app = create_app(db_path=tmp_db, pane_map={}, port=unused_tcp_port, watchdog_disabled=True,
                     anomaly_disabled=True, push_fn=lambda *a, **k: None)
    with TestClient(app) as client:
        response = client.post("/tasks/review-approve/result", json={
            "task_id": "review-approve", "status": "completed",
            "summary": "looks good", "verdict": "approve", "pr_number": 5652})
        assert response.status_code == 200, response.text
    tests.extend(t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "test")
    return posted, tests, merged


def test_a_stale_approval_does_not_enqueue_a_tester(tmp_db, monkeypatch, *, unused_tcp_port):
    """★★P1. The verdict approves `5b496a9f`; the head is `28419e96`. Spending a
    tester on it treats an approval of the old commit as approval of the PR."""
    posted, tests, _ = _submit_approve(tmp_db, monkeypatch, MOVED_TO, unused_tcp_port=unused_tcp_port)
    assert posted == []
    assert tests == [], f"a stale approval still enqueued a tester: {tests}"


def test_a_current_approval_still_enqueues_a_tester(tmp_db, monkeypatch, *, unused_tcp_port):
    """⛔The control. Gating everything would stop the pipeline, not fix it."""
    _, tests, _ = _submit_approve(tmp_db, monkeypatch, PINNED, unused_tcp_port=unused_tcp_port)
    assert len(tests) == 1


def test_a_stale_approval_with_no_tester_does_not_merge(tmp_db, monkeypatch, *, unused_tcp_port):
    """★★The worst case in the finding. `no_tester=True` sends an approval
    straight to merge — so a review of a commit nobody is looking at could land
    a PR whose head had moved twice."""
    _, _, merged = _submit_approve(tmp_db, monkeypatch, MOVED_TO, no_tester=True, unused_tcp_port=unused_tcp_port)
    assert merged == [], f"a stale approval merged a moved PR: {merged}"


def test_the_suppressed_approval_is_recorded(tmp_db, monkeypatch, *, unused_tcp_port):
    from agent_crew.queue import TaskQueue

    _submit_approve(tmp_db, monkeypatch, MOVED_TO, unused_tcp_port=unused_tcp_port)
    ctx = TaskQueue(tmp_db).get_task_context("review-approve")
    assert ctx.get("review_publication") == "stale"


# ── 8. review of PR #306: the expected pin had no reader ──────────────
#
# P1. `_requeue_review_at_head` wrote `expected_head_sha` onto the requeued
# task, but worktree prep reads only `reviewed_sha` — so the field had no
# production reader at all and the requeued review resolved the PR's MOVING
# branch instead of the head it was created for.


def _prep_with(context, *, resolvable):
    """Run reviewer prep with git stubbed; `resolvable` lists refs that exist."""
    from unittest.mock import MagicMock, patch

    from agent_crew.server import _prepare_worktree_for_task

    cmds = []

    def fake_run(cmd, **_kw):
        cmds.append(cmd)
        if "rev-parse" in cmd and "--verify" in cmd:
            ref = cmd[-1].replace("^{commit}", "")
            if ref in resolvable:
                return MagicMock(returncode=0, stdout=resolvable[ref] + "\n", stderr="")
            return MagicMock(returncode=1, stdout="", stderr="unknown revision")
        if "rev-parse" in cmd:
            return MagicMock(returncode=0, stdout=(PINNED + "\n"), stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("agent_crew.server.subprocess.run", side_effect=fake_run):
        _prepare_worktree_for_task("/wt/codex", "review-5652-b", "feat/x", "reviewer",
                                   task_context=context)
    return [c[-1] for c in cmds if "checkout" in c and "--detach" in c]


def test_the_expected_head_is_the_checkout_pin(tmp_db):
    """★★The B-to-C advance. The review was requeued for head B; by dispatch the
    PR is at C. It must be prepared at B — the head it was created for — not at
    whatever the branch resolves to now."""
    detached = _prep_with(
        {"expected_head_sha": MOVED_TO, "pr_number": 5652},
        resolvable={MOVED_TO: MOVED_TO, "origin/feat/x": MOVED_AGAIN,
                    "origin/main": PINNED, MOVED_AGAIN: MOVED_AGAIN})
    assert detached and detached[0] == MOVED_TO, \
        f"prepared at {detached[:1]} instead of the expected head"


def test_an_expected_head_that_does_not_resolve_is_refused(tmp_db):
    """⛔Fail closed, like #289 and #304. Falling back to the moving branch is
    exactly the substitution the expected pin exists to prevent — and doing it
    silently is how #304 happened in the first place."""
    from agent_crew.server import WorktreeTargetUnresolved

    with pytest.raises(WorktreeTargetUnresolved):
        _prep_with({"expected_head_sha": MOVED_TO, "pr_number": 5652},
                   resolvable={"origin/feat/x": MOVED_AGAIN, "origin/main": PINNED})


def test_a_task_with_no_expected_head_is_unaffected(tmp_db):
    """⛔The compatibility control: every review that is not a requeue carries no
    expected head and must prepare exactly as before."""
    detached = _prep_with(
        {},
        resolvable={"origin/feat/x": MOVED_AGAIN, "origin/main": PINNED})
    # Prep resolves the ref to a commit and detaches there (#286), so landing on
    # whatever `origin/feat/x` points at IS the unchanged branch behaviour.
    assert detached and detached[0] == MOVED_AGAIN


def test_the_requeued_task_carries_a_pin_prep_can_actually_use(tmp_db, monkeypatch, *, unused_tcp_port):
    """★★The end of the finding: the field must reach a reader. Asserted against
    prep itself, not against the string being present in the context."""
    from agent_crew.queue import TaskQueue

    _submit_review(tmp_db, monkeypatch, MOVED_TO, unused_tcp_port=unused_tcp_port)
    requeued = [t for t in TaskQueue(tmp_db).list_tasks()
                if t.task_id == stale_review_task_id(5652, MOVED_TO)][0]
    detached = _prep_with(requeued.context,
                          resolvable={MOVED_TO: MOVED_TO, "origin/main": PINNED})
    assert detached and detached[0] == MOVED_TO


def test_a_non_review_result_still_submits(tmp_db, *, unused_tcp_port):
    """⛔The regression that reached a push. The approve gate reads `_pub`
    unconditionally, and binding it only inside the review branch made EVERY
    non-review result raise UnboundLocalError — 59 suites' worth, and the
    targeted tests all passed because they only ever posted review results.

    A gate on one task type must not be able to break the others.
    """
    from fastapi.testclient import TestClient

    from agent_crew.protocol import TaskRequest
    from agent_crew.queue import TaskQueue
    from agent_crew.server import create_app

    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="impl-plain", task_type="implement",
                          description="do a thing", branch="main", context={}))
    app = create_app(db_path=tmp_db, pane_map={}, port=unused_tcp_port, watchdog_disabled=True,
                     anomaly_disabled=True, push_fn=lambda *a, **k: None)
    with TestClient(app) as client:
        response = client.post("/tasks/impl-plain/result", json={
            "task_id": "impl-plain", "status": "completed", "summary": "done"})
    assert response.status_code == 200, response.text
    assert TaskQueue(tmp_db).get_result("impl-plain") is not None
