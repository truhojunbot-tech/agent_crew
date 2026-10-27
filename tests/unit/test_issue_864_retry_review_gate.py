"""#864: a retry review keeps the implementer's independent-review identity."""

import pytest

from agent_crew import github
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


SHA = "a" * 40


def _task(queue, task_id, task_type, context=None):
    queue.enqueue(TaskRequest(
        task_id=task_id, task_type=task_type, description=task_id,
        branch="fix/864", project="agent_crew", context=context or {},
    ))


def _approved_retry(queue, parent_key="prev_task_id"):
    _task(queue, "retry-review", "review", {
        "pr_number": 865, "reviewed_sha": SHA, parent_key: "failed-review",
    })
    if parent_key != "prev_task_id":
        # Public enqueue strips untrusted lineage markers; the cloud retry
        # records these markers after admission.
        queue.patch_context("retry-review", {parent_key: "failed-review"})
    with queue._connect() as db:
        db.execute("UPDATE tasks SET status='completed', verdict='approve', "
                   "pr_number=865 WHERE task_id='retry-review'")
    queue.record_attribution("retry-review", agent="claude", task_type="review")


@pytest.mark.parametrize("parent_key", ["prev_task_id", "retry_of", "original_task_id"])
def test_retry_review_finds_implement_author(tmp_path, monkeypatch, parent_key):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _task(queue, "implement", "implement")
    _task(queue, "failed-review", "review", {"prev_task_id": "implement"})
    queue.record_attribution("implement", agent="codex", task_type="implement")
    _approved_retry(queue, parent_key)
    monkeypatch.setattr(github, "pr_head_sha", lambda *_args, **_kwargs: SHA)

    assert github.independent_review_for_head(queue, 865, "owner/repo") == (
        SHA, "claude", "retry-review", "independent approval")


def test_retry_review_by_implementer_is_refused(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _task(queue, "implement", "implement")
    _task(queue, "failed-review", "review", {"prev_task_id": "implement"})
    queue.record_attribution("implement", agent="claude", task_type="implement")
    _approved_retry(queue)
    monkeypatch.setattr(github, "pr_head_sha", lambda *_args, **_kwargs: SHA)

    assert github.independent_review_for_head(queue, 865, "owner/repo") == (
        "", "", "", "reviewer agent equals implementer agent (claude)")


def test_retry_review_without_implement_in_bounded_lineage_is_refused(
        tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _task(queue, "distant-implement", "implement")
    queue.record_attribution("distant-implement", agent="codex", task_type="implement")
    _task(queue, "review-0", "review", {"prev_task_id": "distant-implement"})
    for index in range(1, 12):
        _task(queue, f"review-{index}", "review",
              {"prev_task_id": f"review-{index - 1}"})
    _task(queue, "failed-review", "review", {"prev_task_id": "review-11"})
    _approved_retry(queue)
    monkeypatch.setattr(github, "pr_head_sha", lambda *_args, **_kwargs: SHA)

    assert github.independent_review_for_head(queue, 865, "owner/repo") == (
        "", "", "", "author or reviewer attribution unavailable")
