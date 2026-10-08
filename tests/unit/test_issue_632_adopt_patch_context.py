"""A CLI-adopted review keeps the user's reviewer and test-stage choices (#632)."""

import io
import urllib.error
from unittest.mock import patch

import pytest

from agent_crew.loop import enqueue_review
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


@pytest.mark.parametrize("transport", ["in_process", "http"])
def test_race_adoption_patches_cli_review_context(tmp_path, monkeypatch, transport):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    server_id = "review-server-632"
    queue.enqueue(TaskRequest(
        task_id=server_id, task_type="review", description="server review",
        branch="fix/632", project="agent_crew",
        context={"prev_task_id": "impl-632", "reviewed_sha": "a" * 40,
                 "pr_number": 632, "agent_override": "server-default"}))
    # The server wins after the CLI's initial lookup, before its enqueue.
    list_tasks = queue.list_tasks
    first_lookup = True

    def stale_once():
        nonlocal first_lookup
        if first_lookup:
            first_lookup = False
            return []
        return list_tasks()

    monkeypatch.setattr(queue, "list_tasks", stale_once)
    cli_context = {"reviewed_sha": "a" * 40, "pr_number": 632,
                   "no_tester": True, "agent_override": "claude"}
    if transport == "http":
        body = io.BytesIO(
            b'{"detail":{"error":"DUPLICATE_REVIEW",'
            b'"existing_task_id":"review-server-632"}}')
        conflict = urllib.error.HTTPError("http://localhost/tasks", 409,
                                          "Conflict", {}, body)
        with patch("agent_crew.loop._post_task_http", side_effect=conflict):
            adopted = enqueue_review(
                queue, "work", "fix/632", "impl-632", cli_context,
                port=8105, project="agent_crew")
    else:
        adopted = enqueue_review(
            queue, "work", "fix/632", "impl-632", cli_context,
            project="agent_crew")

    assert adopted == server_id
    context = queue.get_task_context(server_id)
    assert context["no_tester"] is True
    assert context["agent_override"] == "claude"
    assert context["prev_task_id"] == "impl-632"
    assert context["reviewed_sha"] == "a" * 40
