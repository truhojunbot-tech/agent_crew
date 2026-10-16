"""Cloud credit attribution labels every terminal unknown (#783)."""

import json
import sqlite3
import time

import pytest

from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


@pytest.mark.parametrize("shadow", [False, True], ids=["normal", "shadow"])
@pytest.mark.parametrize("before,after,overlap,status,usd", [
    (4.0, 4.5, False, "MEASURED", 0.5),
    (4.0, 4.5, True, "UNKNOWN_OVERLAP", None),
    (None, 4.5, False, "UNKNOWN_NO_FRESH_READING", None),
    (4.0, None, False, "UNKNOWN_NO_FRESH_READING", None),
    (5.0, 4.5, False, "UNKNOWN_COUNTER_DECREASED", None),
    (5.0, 4.5, True, "UNKNOWN_OVERLAP", None),
    (None, None, True, "UNKNOWN_OVERLAP", None),
])
def test_terminal_cloud_cost_status_and_overlaps(
        tmp_path, monkeypatch, shadow, before, after, overlap, status, usd):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    task_type = "review" if shadow else "implement"
    queue.enqueue(TaskRequest(task_id="target", task_type=task_type,
                              description="Cloud task", branch="agent/target"))
    started = time.time() - 10
    monkeypatch.setenv("AGENT_CREW_CLOUD_COST_SETTLE_SECONDS", "0")
    prefix = "cloud_shadow_" if shadow else "cloud_"
    queue.patch_context("target", {
        prefix + "attempt_started_at": started,
        prefix + "ended_at": time.time() - 1,
        prefix + "credit_used_before": before,
        prefix + "credit_before_fetched_at": started - 1,
    })
    attribution_id = "cloud-shadow:target" if shadow else "target"
    queue.record_attribution(task_id=attribution_id, project="agent_crew",
                             agent="claude_cloud",
                             role="reviewer" if shadow else "implementer",
                             task_type=task_type, started_at=started)
    if overlap:
        queue.enqueue(TaskRequest(task_id="other", task_type="implement",
                                  description="Overlapping cloud session",
                                  branch="agent/other",
                                  context={"cloud_attempt_started_at": started + 1}))
    monkeypatch.setattr(cloud, "read_cloud_credit_used",
                        lambda: cloud.CloudCreditSnapshot(after, time.time()) if after is not None else None)

    if shadow:
        cloud._record_shadow_review_cost(queue, "target")
        assert queue.get_attribution(attribution_id)["cloud_cost_status"] == "PENDING_SETTLE"
        cloud._record_shadow_review_cost(queue, "target")
    else:
        cloud._record_reconciled_cloud_cost(queue, queue.get_task("target"))
        assert queue.get_attribution(attribution_id)["cloud_cost_status"] == "PENDING_SETTLE"
        cloud._record_reconciled_cloud_cost(queue, queue.get_task("target"))

    attr = queue.get_attribution(attribution_id)
    assert attr["cloud_cost_status"] == status
    assert json.loads(attr["cloud_cost_overlaps"]) == (["other"] if overlap else [])
    assert attr["cloud_credit_used_before"] == before
    assert attr["cloud_credit_used_after"] == after
    assert attr["cloud_usd"] == usd
    context = queue.get_task_context("target")
    assert context[prefix + "cost_overlaps"] == (["other"] if overlap else [])


def test_cloud_cost_columns_are_nullable_on_existing_attribution(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.record_attribution(task_id="old", project="agent_crew", agent="claude_cloud",
                             role="implementer", task_type="implement")
    with sqlite3.connect(queue._db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(task_attribution)")}
    assert {"cloud_cost_status", "cloud_cost_overlaps"} <= columns
    assert queue.get_attribution("old")["cloud_cost_status"] is None
    assert queue.get_attribution("old")["cloud_cost_overlaps"] is None
