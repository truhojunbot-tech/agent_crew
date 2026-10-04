"""A server-created review or test can win the race with a CLI enqueue."""

from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest

from agent_crew import loop
from agent_crew.protocol import TaskRequest


def _conflict():
    return HTTPError("http://127.0.0.1:8101/tasks", 409, "Conflict", {}, None)


def _task(task_id, task_type, previous):
    return TaskRequest(task_id=task_id, task_type=task_type,
                       description="server successor", context={"prev_task_id": previous})


def test_review_409_returns_server_review_and_skips_dead_review():
    queue = MagicMock()
    dead = _task("review-dead", "review", "impl-1")
    live = _task("review-server", "review", "impl-1")
    queue.list_tasks.side_effect = [[], [dead, live]]
    queue.get_task_status.side_effect = lambda task_id: (
        "failed" if task_id == dead.task_id else "pending")
    conflict = _conflict()

    with patch.object(loop, "_post_task_http", side_effect=conflict):
        task_id = loop.enqueue_review(
            queue, "review work", "feature", "impl-1",
            context={"no_tester": True}, port=8101)

    assert task_id == live.task_id
    queue.patch_context.assert_called_once_with(live.task_id, {"no_tester": True})
    queue.enqueue.assert_not_called()


def test_review_409_without_matching_successor_reraises(monkeypatch):
    queue = MagicMock()
    queue.list_tasks.return_value = []
    monkeypatch.setattr(loop.time, "sleep", lambda _seconds: None)
    conflict = _conflict()

    with patch.object(loop, "_post_task_http", side_effect=conflict):
        with pytest.raises(HTTPError) as raised:
            loop.enqueue_review(queue, "review work", "feature", "impl-1", port=8101)

    assert raised.value is conflict
    assert queue.list_tasks.call_count > 1


def test_test_409_returns_server_test_after_retry(monkeypatch):
    queue = MagicMock()
    existing = _task("test-server", "test", "review-1")
    queue.list_tasks.side_effect = [[], [], [existing]]
    monkeypatch.setattr(loop.time, "sleep", lambda _seconds: None)
    conflict = _conflict()

    with patch.object(loop, "_post_task_http", side_effect=conflict):
        task_id = loop.enqueue_test(queue, "test work", "feature", "review-1", port=8101)

    assert task_id == existing.task_id
    queue.enqueue.assert_not_called()


def test_test_409_without_matching_successor_reraises(monkeypatch):
    queue = MagicMock()
    queue.list_tasks.return_value = []
    monkeypatch.setattr(loop.time, "sleep", lambda _seconds: None)
    conflict = _conflict()

    with patch.object(loop, "_post_task_http", side_effect=conflict):
        with pytest.raises(HTTPError) as raised:
            loop.enqueue_test(queue, "test work", "feature", "review-1", port=8101)

    assert raised.value is conflict
    assert queue.list_tasks.call_count > 1
