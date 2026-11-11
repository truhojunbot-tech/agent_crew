"""#1010: ``record_dispatch`` makes a chatgpt review visible to the reconciler
before its request is posted. A reconcile inside that window must wait, not
end the row ``chatgpt_review_state_missing``; and a row that ends anyway is
never patched by the dispatcher -- its posted request is reported orphaned."""

import pytest

from agent_crew import chatgpt_review as chatgpt
from tests.unit.test_issue_1000_chatgpt_reviewer import (  # noqa: F401 (fixtures)
    PR, REPO, ROLES, _dispatch, _enqueue, gh, notices, queue,
)

pytestmark = pytest.mark.usefixtures("env")


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv(chatgpt._ENV_CARRIER, "pr")
    monkeypatch.setenv(chatgpt._ENV_DISPATCH_GRACE_S, "120")
    monkeypatch.setenv(chatgpt._ENV_POLL_S, "300")


def _reconcile(queue, gh, now):
    def submit(tid, result):
        pytest.fail("unexpected result submission")
    return chatgpt.reconcile_chatgpt_reviews(queue, gh=gh, now=now,
                                             submit_review_result_fn=submit)


def _reconcile_during_post(queue, gh, at):
    """Run one watchdog reconcile inside ``post_comment``: after
    record_dispatch, before the dispatcher records the request state."""
    seen = []
    original = gh.post_comment

    def post(repo, number, body):
        context = queue.get_task(seen_task[0]).context
        assert "chatgpt_request_comment_id" not in context  # really mid-dispatch
        seen.extend(_reconcile(queue, gh, at))
        return original(repo, number, body)
    seen_task = []
    gh.post_comment = post
    return seen, seen_task


def test_default_grace_is_two_minutes(monkeypatch):
    monkeypatch.delenv(chatgpt._ENV_DISPATCH_GRACE_S, raising=False)
    assert chatgpt.dispatch_grace_seconds() == 120.0


# (a) (c) -------------------------------------------------------------------

def test_reconcile_inside_the_post_window_waits_then_polls(queue, gh, notices):
    task_id = _enqueue(queue)
    seen, seen_task = _reconcile_during_post(queue, gh, at=1001.0)
    seen_task.append(task_id)
    assert _dispatch(queue, gh, now=1000.0).action == "dispatched"
    assert [(o.action, o.detail) for o in seen] == [("still_waiting", "dispatch in progress")]
    row = queue.get_task(task_id)
    assert row.status == "in_progress"
    assert row.context["chatgpt_request_comment_id"] == gh.next_id
    assert "chatgpt_last_poll_at" not in row.context  # the grace read nothing
    assert [k for _, k, _ in notices] == []

    # (c) after the dispatcher's patch the next reconcile polls normally.
    reads = []
    original = gh.comments
    gh.comments = lambda repo, number: reads.append((repo, number)) or original(repo, number)
    assert [o.action for o in _reconcile(queue, gh, 1010.0)] == ["still_waiting"]
    assert reads == [(REPO, PR)]
    assert queue.get_task(task_id).context["chatgpt_last_poll_at"] == 1010.0


# (b) (d) -------------------------------------------------------------------

def test_past_grace_ends_state_missing_and_the_request_is_orphaned(queue, gh, notices):
    task_id = _enqueue(queue)
    seen, seen_task = _reconcile_during_post(queue, gh, at=1000.0 + 121.0)
    seen_task.append(task_id)
    outcome = _dispatch(queue, gh, now=1000.0)
    # (b) unchanged behaviour past the grace
    assert [o.action for o in seen] == ["needs_human"]
    row = queue.get_task(task_id)
    assert row.status == "needs_human"
    # (d) the dispatcher did not patch the ended row
    for key in ("chatgpt_thread", "chatgpt_request_comment_id", "chatgpt_request_url",
                "chatgpt_requested_at", "chatgpt_base_sha"):
        assert key not in row.context
    url = f"https://github.com/{REPO}/issues/{PR}#c{gh.next_id}"
    assert (outcome.action, outcome.detail) == ("request_orphaned", url)
    assert [(k, d) for _, k, d in notices] == [
        ("chatgpt_review_state_missing",
         "dispatched review has no recorded request thread/comment"),
        ("chatgpt_review_request_orphaned", url)]
    assert len(gh.posted) == 1


# (e) -----------------------------------------------------------------------

def test_missing_pinned_pr_or_sha_is_not_covered_by_the_grace(queue, gh, notices):
    task_id = _enqueue(queue, reviewed_sha="not-a-sha")
    task = queue.dequeue(agent=chatgpt.PROVIDER_NAME, role="reviewer",
                         claimed_via="chatgpt_push", skip_deferred=True,
                         role_to_agent=dict(ROLES))
    assert task.task_id == task_id
    queue.record_dispatch(task_id, channel=chatgpt.DISPATCH_CHANNEL,
                          agent=chatgpt.PROVIDER_NAME, target=f"{REPO}#{PR}",
                          lease_owner=f"chatgpt:{task_id}", lease_seconds=600, ts=1000.0)
    assert [o.action for o in _reconcile(queue, gh, 1001.0)] == ["needs_human"]
    assert queue.get_task(task_id).status == "needs_human"
    assert [k for _, k, _ in notices] == ["chatgpt_review_state_missing"]
