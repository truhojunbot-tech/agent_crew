"""#926: a merge needs a test verdict about the approved PR head."""

import pytest

from agent_crew.conformance_gate import test_pass_for_head as _test_pass_for_head
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


@pytest.fixture
def lineage(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("review-926", "review", "review", branch="topic",
                              context={"pr_number": 926, "reviewed_sha": "a" * 40}))
    queue.enqueue(TaskRequest("test-926", "test", "test", branch="topic",
                              context={"prev_task_id": "review-926", "pr_number": 926,
                                       "reviewed_sha": "a" * 40}))
    return queue


def test_unrun_or_prose_only_test_cannot_authorize_merge(lineage):
    assert not _test_pass_for_head(lineage, "review-926", 926, "a" * 40)
    lineage.submit_result("test-926", TaskResult("test-926", "completed",
                                                   "all tests passed", pr_number=926))
    assert not _test_pass_for_head(lineage, "review-926", 926, "a" * 40)


@pytest.mark.parametrize("outcome,head,expected", [
    ("PASSED", "a" * 40, True),
    ("PASSED", "b" * 40, False),
    ("INCOMPLETE", "a" * 40, False),
    ("FAILED", "a" * 40, False),
])
def test_only_structured_pass_for_reviewed_head_authorizes_merge(
        tmp_path, outcome, head, expected):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("review-926", "review", "review", branch="topic",
                              context={"pr_number": 926, "reviewed_sha": "a" * 40}))
    queue.enqueue(TaskRequest("test-926", "test", "test", branch="topic",
                              context={"prev_task_id": "review-926", "pr_number": 926,
                                       "reviewed_sha": "a" * 40}))
    queue.submit_result("test-926", TaskResult(
        "test-926", "completed", "done", pr_number=926,
        error_info={"script_test": {"outcome": outcome, "head_sha": head}}))
    assert _test_pass_for_head(queue, "review-926", 926, "a" * 40) is expected
