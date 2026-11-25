"""#1036: a ChatGPT reply tagged with an already-terminal sibling review's task id
is adopted by the open same-PR/same-sha review in its lineage, once, and only
when it is newer than that sibling's posted result. Replay of PR 1034:
``review-impl-1033-r0`` ended request_changes, ChatGPT then posted an approve
still tagged ``r0``, and the re-review ``r1`` timed out 12h later."""

import time

import pytest

from agent_crew import chatgpt_review as chatgpt
from agent_crew.protocol import TaskRequest
from tests.unit.test_issue_1000_chatgpt_reviewer import (  # noqa: F401 (fixtures)
    FINDING, OTHER_SHA, PR, REPO, SHA, FakeGh, _dispatch, _enqueue, _reply, _thread,
    carrier, gh, notices, queue,
)

R0 = "review-impl-1000-r0"
R1 = "review-impl-1000-r1"
OTHER_PR = 1001


def _submit_into(queue):
    """A result handler that really ends the row, so ``result_posted_at`` exists."""
    submitted = []

    def submit(task_id, result):
        submitted.append((task_id, result))
        queue.submit_result(task_id, result)
        return {"status": "ok"}
    return submit, submitted


def _close_r0_request_changes(queue, gh, carrier):
    """r0 dispatched, answered request_changes, ended -- the standing terminal review."""
    _enqueue(queue, task_id=R0)
    assert _dispatch(queue, gh).action == "dispatched"
    gh.reply(*_thread(carrier), _reply(task_id=R0))
    submit, _ = _submit_into(queue)
    outcomes = chatgpt.reconcile_chatgpt_reviews(queue, gh=gh, submit_review_result_fn=submit)
    assert [o.action for o in outcomes] == ["review_completed"]
    assert queue.get_task(R0).status == "completed"
    posted_at = queue.get_exec_state(R0)["result_posted_at"]
    assert isinstance(posted_at, float)
    return posted_at


def _open_r1(queue, gh, *, now, pr_number=PR, sha=SHA):
    context = {"pr_number": pr_number, "reviewed_sha": sha, "repo": REPO, "prev_task_id": R0,
               "lineage_root_task_id": "impl-1000", "issue_number": 1000,
               "implementer_agent": "claude",
               # how a coordinator admits a same-sha re-review past the duplicate gate
               "allow_duplicate_review": True}
    queue.enqueue(TaskRequest(task_id=R1, task_type="review", description="Re-review PR",
                              branch="agent/chatgpt", project="agent_crew", context=context))
    outcome = _dispatch(queue, gh, now=now)
    assert outcome.action == "dispatched", outcome
    return queue.get_task(R1).context["chatgpt_request_comment_id"]


def _reconcile_r1(queue, gh, *, now):
    submit, submitted = _submit_into(queue)
    outcomes = chatgpt.reconcile_chatgpt_reviews(queue, gh=gh, now=now,
                                                 submit_review_result_fn=submit)
    return [o for o in outcomes if o.task_id == R1], submitted


def _adopted_events(queue, task_id=R1):
    return [e for e in queue.get_exec_state(task_id)["events"]
            if e["event"] == chatgpt.SIBLING_ADOPTED_EVENT]


# --- the PR 1034 replay --------------------------------------------------------

def test_approve_tagged_with_closed_sibling_completes_the_open_re_review(queue, gh, notices,
                                                                          carrier):
    posted_at = _close_r0_request_changes(queue, gh, carrier)
    # ChatGPT's later approve, still tagged r0, lands BEFORE r1's request is posted.
    gh.reply(*_thread(carrier), _reply(task_id=R0, verdict="VERDICT: approve",
                                        text="re-review: findings addressed"),
             created_at=posted_at + 100)
    approve_id = gh.next_id
    request_id = _open_r1(queue, gh, now=posted_at + 200)
    assert approve_id < request_id

    outcomes, submitted = _reconcile_r1(queue, gh, now=posted_at + 300)
    assert [o.action for o in outcomes] == ["review_completed"]
    assert outcomes[0].detail == "approve"
    (task_id, result), = submitted
    assert task_id == R1 and result.verdict == "approve" and result.pr_number == PR
    assert result.findings == [] and "findings addressed" in result.summary
    row = queue.get_task(R1)
    assert row.status == "completed" and row.verdict == "approve"
    assert row.context["chatgpt_reply_comment_id"] == approve_id
    assert row.context["chatgpt_sibling_reply_adopted"]["sibling_task_id"] == R0
    event, = _adopted_events(queue)
    assert event["comment_id"] == approve_id
    assert event["sibling_task_id"] == R0 and event["verdict"] == "approve"
    assert event["via"] == "sibling_reply" and event.get("disposition_comment_id") is None
    assert event["sibling_result_posted_at"] == posted_at
    # r0 keeps its own result; nothing was replayed onto it.
    assert queue.get_task(R0).verdict == "request_changes"
    assert _adopted_events(queue, R0) == []
    assert notices == []


def test_duplicate_of_disposition_names_the_adopted_sibling_reply(queue, gh, notices, carrier):
    posted_at = _close_r0_request_changes(queue, gh, carrier)
    gh.reply(*_thread(carrier), _reply(task_id=R0, verdict="VERDICT: approve",
                                        text="re-review: findings addressed"),
             created_at=posted_at + 100)
    approve_id = gh.next_id
    _open_r1(queue, gh, now=posted_at + 200)
    # ChatGPT's answer to r1's request, in the live format of c6960702246.
    gh.reply(*_thread(carrier), "\n".join([
        f"{chatgpt.DISPOSITION_PREFIX} task={R1} sha={SHA}",
        "QUEUE_MISS / SAME_SHA_DUPLICATE_REQUEST — no new technical verdict.",
        "",
        f"Canonical disposition for r1: DUPLICATE_OF task={R0}, result "
        f"https://github.com/{REPO}/pull/{PR}#issuecomment-{approve_id}. This is not a new "
        "[CHATGPT][CREW-REVIEW] verdict.",
    ]), created_at=posted_at + 250)
    disposition_id = gh.next_id

    outcomes, submitted = _reconcile_r1(queue, gh, now=posted_at + 300)
    assert [o.action for o in outcomes] == ["review_completed"]
    assert submitted[0][1].verdict == "approve"
    assert queue.get_task(R1).context["chatgpt_reply_comment_id"] == approve_id
    event, = _adopted_events(queue)
    assert event["comment_id"] == approve_id
    assert event["via"] == "duplicate_of_disposition"
    assert event["disposition_comment_id"] == disposition_id


def test_disposition_pointing_at_an_ineligible_sibling_adopts_nothing(queue, gh, notices,
                                                                      carrier):
    posted_at = _close_r0_request_changes(queue, gh, carrier)
    _open_r1(queue, gh, now=posted_at + 200)
    gh.reply(*_thread(carrier), "\n".join([
        f"{chatgpt.DISPOSITION_PREFIX} task={R1} sha={SHA}",
        f"DUPLICATE_OF task={R0}, result https://github.com/{REPO}/pull/{PR}#issuecomment-1",
    ]), created_at=posted_at + 250)
    # r0's only reply is the request_changes that ended it: not newer than its result.
    outcomes, submitted = _reconcile_r1(queue, gh, now=posted_at + 300)
    assert [o.action for o in outcomes] == ["still_waiting"]
    assert submitted == [] and _adopted_events(queue) == []
    assert queue.get_task(R1).status == "in_progress"


# --- fail-closed --------------------------------------------------------------

@pytest.mark.parametrize("case", ["older_than_sibling_result", "different_sha",
                                  "not_a_terminal_sibling", "garbled_verdict"])
def test_sibling_reply_is_not_adopted_when_it_fails_a_check(queue, gh, notices, carrier, case):
    posted_at = _close_r0_request_changes(queue, gh, carrier)
    if case == "older_than_sibling_result":
        gh.reply(*_thread(carrier), _reply(task_id=R0, verdict="VERDICT: approve", text=""),
                 created_at=posted_at - 1)
    elif case == "different_sha":
        gh.reply(*_thread(carrier), _reply(task_id=R0, sha=OTHER_SHA, verdict="VERDICT: approve",
                                            text=""), created_at=posted_at + 100)
    elif case == "not_a_terminal_sibling":
        gh.reply(*_thread(carrier), _reply(task_id="review-other-r0", verdict="VERDICT: approve",
                                            text=""), created_at=posted_at + 100)
    else:
        gh.reply(*_thread(carrier), _reply(task_id=R0, verdict="VERDICT: approve\nVERDICT: approve",
                                            text=""), created_at=posted_at + 100)
    _open_r1(queue, gh, now=posted_at + 200)

    outcomes, submitted = _reconcile_r1(queue, gh, now=posted_at + 300)
    assert [o.action for o in outcomes] == ["still_waiting"]
    assert submitted == [] and _adopted_events(queue) == []
    assert queue.get_task(R1).status == "in_progress"
    assert notices == []  # a sibling's reply never sends this row to a human


def test_sibling_reply_on_a_different_pr_is_not_adopted(queue, gh, notices, monkeypatch):
    # The shared intake thread is where a reply for another PR can be seen at all.
    monkeypatch.setenv(chatgpt._ENV_CARRIER, "intake")
    monkeypatch.setenv(chatgpt._ENV_INTAKE, "owner/intake#7")
    posted_at = _close_r0_request_changes(queue, gh, "intake")  # r0 is pinned to PR 1000
    gh.reply("owner/intake", 7, _reply(task_id=R0, verdict="VERDICT: approve", text=""),
             created_at=posted_at + 100)
    _open_r1(queue, gh, now=posted_at + 200, pr_number=OTHER_PR)  # same lineage, other PR

    outcomes, submitted = _reconcile_r1(queue, gh, now=posted_at + 300)
    assert [o.action for o in outcomes] == ["still_waiting"]
    assert submitted == [] and _adopted_events(queue) == []


def test_sibling_reply_after_the_deadline_still_times_out(queue, gh, notices, carrier):
    posted_at = _close_r0_request_changes(queue, gh, carrier)
    _open_r1(queue, gh, now=posted_at + 200)
    deadline = posted_at + 200 + chatgpt.timeout_seconds()
    gh.reply(*_thread(carrier), _reply(task_id=R0, verdict="VERDICT: approve", text=""),
             created_at=deadline + 1)
    outcomes, submitted = _reconcile_r1(queue, gh, now=deadline + 5)
    assert [o.action for o in outcomes] == ["timed_out"]
    assert submitted == [] and _adopted_events(queue) == []
    assert queue.get_task(R1).status == "timed_out"


def test_own_reply_wins_over_a_sibling_reply(queue, gh, notices, carrier):
    posted_at = _close_r0_request_changes(queue, gh, carrier)
    gh.reply(*_thread(carrier), _reply(task_id=R0, verdict="VERDICT: approve", text=""),
             created_at=posted_at + 100)
    _open_r1(queue, gh, now=posted_at + 200)
    gh.reply(*_thread(carrier), _reply(task_id=R1, text=FINDING), created_at=posted_at + 250)
    own_id = gh.next_id
    outcomes, submitted = _reconcile_r1(queue, gh, now=posted_at + 300)
    assert [o.action for o in outcomes] == ["review_completed"]
    assert submitted[0][1].verdict == "request_changes"
    assert queue.get_task(R1).context["chatgpt_reply_comment_id"] == own_id
    assert _adopted_events(queue) == []


def test_one_reply_does_not_end_two_rows(queue, gh, notices, carrier):
    """r0 still open when its reply lands: r0 takes it, r1 does not adopt it."""
    _enqueue(queue, task_id=R0)
    assert _dispatch(queue, gh).action == "dispatched"
    _open_r1(queue, gh, now=time.time())
    gh.reply(*_thread(carrier), _reply(task_id=R0, verdict="VERDICT: approve", text=""))
    submit, submitted = _submit_into(queue)
    outcomes = chatgpt.reconcile_chatgpt_reviews(queue, gh=gh, submit_review_result_fn=submit)
    by_task = {o.task_id: o.action for o in outcomes}
    assert by_task == {R0: "review_completed", R1: "still_waiting"}
    assert [tid for tid, _ in submitted] == [R0]
    assert _adopted_events(queue) == []


def test_held_adoption_keeps_waiting_and_records_no_event(queue, gh, notices, carrier):
    posted_at = _close_r0_request_changes(queue, gh, carrier)
    gh.reply(*_thread(carrier), _reply(task_id=R0, verdict="VERDICT: approve", text=""),
             created_at=posted_at + 100)
    _open_r1(queue, gh, now=posted_at + 200)
    outcomes = chatgpt.reconcile_chatgpt_reviews(
        queue, gh=gh, now=posted_at + 300,
        submit_review_result_fn=lambda tid, result: {"held": "no_artifact"})
    r1, = [o for o in outcomes if o.task_id == R1]
    assert r1.action == "still_waiting" and "held" in r1.detail
    assert queue.get_task(R1).status == "in_progress"
    assert _adopted_events(queue) == []


def test_record_exec_event_appends_to_the_task_history(queue):
    queue.enqueue(TaskRequest(task_id="t1", task_type="implement", description="x"))
    queue.record_exec_event("t1", "sibling_reply_adopted", at=123.0, comment_id=7, via="x",
                            disposition_comment_id=None)
    event, = [e for e in queue.get_exec_state("t1")["events"] if e["event"] == "sibling_reply_adopted"]
    assert event["at"] == 123.0 and event["comment_id"] == 7 and event["via"] == "x"
    assert "disposition_comment_id" not in event  # None fields are dropped, as elsewhere
