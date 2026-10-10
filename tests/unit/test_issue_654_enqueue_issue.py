"""Issue links are queryable; missing links warn without refusing enqueue."""

import logging
import sqlite3

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


def test_direct_enqueue_without_issue_warns_but_admits(tmp_path, caplog):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    with caplog.at_level(logging.WARNING, logger="agent_crew.queue"):
        task_id = queue.enqueue(TaskRequest("impl-no-issue", "implement", "work",
                                            context={"risk_tier": 1}))
    assert task_id == "impl-no-issue"
    assert queue.get_task(task_id).status == "pending"
    assert "without issue_number" in caplog.text


def test_direct_enqueue_issue_number_is_queryable(tmp_path, caplog):
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    with caplog.at_level(logging.WARNING, logger="agent_crew.queue"):
        queue.enqueue(TaskRequest("impl-with-issue", "implement", "work",
                                  context={"issue_number": 654, "risk_tier": 1}))
    context = queue.get_task_context("impl-with-issue")
    assert context["issue"] == 654
    assert context["issue_number"] == 654
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT task_id FROM tasks WHERE json_extract(context, '$.issue_number') = ?",
            (654,),
        ).fetchall()
    assert rows == [("impl-with-issue",)]
    assert "without issue_number" not in caplog.text
