"""Keep the legacy test fixture from altering production successor inheritance."""

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue, _CEA_SYSTEM_SUCCESSOR_PROVENANCE


def test_tier_three_successor_inherits_parent_tier_under_legacy_fixture(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("parent-928-fixture", "implement", "work",
                              context={"risk_tier": 3}))
    queue.enqueue(TaskRequest("child-928-fixture", "review", "review",
                              context={"prev_task_id": "parent-928-fixture"}),
                  ingress="cascade.review",
                  _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
    assert queue.get_task_context("child-928-fixture")["risk_tier"] == 3


def test_legacy_fixture_still_declares_tier_for_test_authored_root(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("root-928-fixture", "implement", "work"),
                  ingress="cli.enqueue")
    assert queue.get_task_context("root-928-fixture")["risk_tier"] == 1
