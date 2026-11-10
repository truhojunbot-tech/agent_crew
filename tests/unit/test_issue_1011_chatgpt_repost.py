"""#1011: an unanswered chatgpt review request is re-posted exactly once,
after ``AGENT_CREW_CHATGPT_REVIEW_REPOST_S``, on the same thread. The deadline
and the accepted-reply window stay anchored on the first request."""

import pytest

from agent_crew import chatgpt_review as chatgpt
from tests.unit.test_issue_1000_chatgpt_reviewer import (  # noqa: F401 (fixtures)
    PR, REPO, SHA, _dispatch, _enqueue, _iso, _reply, gh, notices, queue,
)

pytestmark = pytest.mark.usefixtures("env")


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv(chatgpt._ENV_CARRIER, "pr")
    monkeypatch.setenv(chatgpt._ENV_REPOST_S, "1000")
    monkeypatch.setenv(chatgpt._ENV_TIMEOUT_S, "5000")


def _reconcile(queue, gh, now, submitted=None):
    def submit(tid, result):
        if submitted is None:
            pytest.fail("unexpected result submission")
        submitted.append(result)
        return {"status": "ok"}
    return [o.action for o in chatgpt.reconcile_chatgpt_reviews(
        queue, gh=gh, now=now, submit_review_result_fn=submit)]


def _reposts(notices):
    return [detail for _, kind, detail in notices if kind == "chatgpt_review_reposted"]


def test_default_repost_is_three_hours(monkeypatch):
    monkeypatch.delenv(chatgpt._ENV_REPOST_S, raising=False)
    assert chatgpt.repost_seconds() == 10800.0


# (a) (b) -------------------------------------------------------------------

def test_unanswered_request_is_reposted_exactly_once(queue, gh, notices):
    task_id = _enqueue(queue)
    _dispatch(queue, gh, now=1000.0)
    first = queue.get_task(task_id).context
    assert _reconcile(queue, gh, 1500.0) == ["still_waiting"]
    assert len(gh.posted) == 1  # before REPOST_S: nothing

    assert _reconcile(queue, gh, 2000.0) == ["still_waiting"]
    assert len(gh.posted) == 2
    (repo1, num1, body1), (repo2, num2, body2) = gh.posted
    assert (repo2, num2) == (repo1, num1) == (REPO, PR)
    assert body2.splitlines()[0] == body1.splitlines()[0]  # same task, same sha
    assert f"sha={SHA}" in body2.splitlines()[0]
    context = queue.get_task(task_id).context
    assert context["chatgpt_reposted_at"] == 2000.0
    assert context["chatgpt_repost_comment_id"] == gh.next_id
    assert context["chatgpt_repost_url"].endswith(f"#c{gh.next_id}")
    assert context["chatgpt_requested_at"] == first["chatgpt_requested_at"] == 1000.0
    assert context["chatgpt_request_comment_id"] == first["chatgpt_request_comment_id"]
    assert "chatgpt_repost_failed" not in context
    (detail,) = _reposts(notices)
    assert detail == f"{first['chatgpt_request_url']} -> {context['chatgpt_repost_url']}"

    for now in (2400.0, 3000.0, 4000.0):
        assert _reconcile(queue, gh, now) == ["still_waiting"]
    assert len(gh.posted) == 2
    assert len(_reposts(notices)) == 1


# (c) (d) -------------------------------------------------------------------

@pytest.mark.parametrize("answers", ["first", "repost"])
def test_reply_to_either_request_is_accepted(queue, gh, notices, answers):
    task_id = _enqueue(queue)
    _dispatch(queue, gh, now=1000.0)
    first_id = queue.get_task(task_id).context["chatgpt_request_comment_id"]
    gh.next_id += 10  # leave id room between the two requests
    _reconcile(queue, gh, 2000.0)
    repost_id = queue.get_task(task_id).context["chatgpt_repost_comment_id"]
    body = _reply(verdict="VERDICT: approve", text="")
    if answers == "first":  # posted under the first request, read after the re-post
        gh.threads[(REPO, PR)].append({"id": first_id + 5, "body": body,
                                       "created_at": _iso(1990.0)})
    else:
        gh.reply(REPO, PR, body, created_at=2100.0)
        assert gh.next_id > repost_id
    submitted = []
    assert _reconcile(queue, gh, 2400.0, submitted) == ["review_completed"]
    assert [r.verdict for r in submitted] == ["approve"]
    assert len(gh.posted) == 2


# (e) -----------------------------------------------------------------------

def test_reply_before_repost_time_means_no_repost(queue, gh, notices):
    _enqueue(queue)
    _dispatch(queue, gh, now=1000.0)
    gh.reply(REPO, PR, _reply(), created_at=1500.0)
    submitted = []
    assert _reconcile(queue, gh, 2500.0, submitted) == ["review_completed"]
    assert len(gh.posted) == 1 and _reposts(notices) == []
    assert [r.verdict for r in submitted] == ["request_changes"]


# (f) -----------------------------------------------------------------------

def test_timeout_is_measured_from_the_first_request(queue, gh, notices):
    task_id = _enqueue(queue)
    _dispatch(queue, gh, now=1000.0)
    _reconcile(queue, gh, 2000.0)
    assert len(gh.posted) == 2
    gh.reply(REPO, PR, _reply(verdict="VERDICT: approve", text=""), created_at=6001.0)
    assert _reconcile(queue, gh, 6001.0) == ["timed_out"]
    assert queue.get_task(task_id).status == "timed_out"
    assert len(gh.posted) == 2


# (g) -----------------------------------------------------------------------

@pytest.mark.parametrize("broken", ["closed", "merged", "pull_none", "files_none"])
def test_unreadable_or_closed_pr_does_not_repost(queue, gh, notices, monkeypatch, broken):
    task_id = _enqueue(queue)
    _dispatch(queue, gh, now=1000.0)
    original_pull, original_files = gh.pull, gh.pull_files
    if broken in ("closed", "merged"):
        gh.pull = lambda *a: {**original_pull(*a), "state": "closed",
                              "merged": broken == "merged"}
    elif broken == "pull_none":
        gh.pull = lambda *a: None
    else:
        gh.pull_files = lambda *a: None
    assert _reconcile(queue, gh, 2000.0) == ["still_waiting"]
    assert len(gh.posted) == 1 and _reposts(notices) == []
    assert "chatgpt_reposted_at" not in queue.get_task(task_id).context
    # No state was spent: once GitHub reads again, the next poll re-posts.
    gh.pull, gh.pull_files = original_pull, original_files
    _reconcile(queue, gh, 2400.0)
    assert len(gh.posted) == 2 and len(_reposts(notices)) == 1


# (h) -----------------------------------------------------------------------

def test_repost_body_is_never_a_reply(queue, gh, notices):
    task_id = _enqueue(queue)
    _dispatch(queue, gh, now=1000.0)
    _reconcile(queue, gh, 2000.0)
    body = gh.posted[1][2]
    assert chatgpt.parse_reply(body, task_id=task_id, sha=SHA).kind == "foreign"
    gh.threads[(REPO, PR)][-1]["created_at"] = _iso(2000.0)  # inside the window
    assert _reconcile(queue, gh, 2400.0) == ["still_waiting"]
    assert queue.get_task(task_id).status == "in_progress"


# (i) -----------------------------------------------------------------------

def test_failed_repost_is_recorded_and_never_retried(queue, gh, notices):
    task_id = _enqueue(queue)
    _dispatch(queue, gh, now=1000.0)
    attempts = []
    gh.post_comment = lambda *a: attempts.append(a) and None
    assert _reconcile(queue, gh, 2000.0) == ["still_waiting"]
    assert len(attempts) == 1
    context = queue.get_task(task_id).context
    assert context["chatgpt_reposted_at"] == 2000.0
    assert context["chatgpt_repost_failed"] is True
    assert "chatgpt_repost_comment_id" not in context
    (detail,) = _reposts(notices)
    assert "FAILED" in detail
    for now in (2400.0, 3000.0):
        _reconcile(queue, gh, now)
    assert len(attempts) == 1 and len(_reposts(notices)) == 1
