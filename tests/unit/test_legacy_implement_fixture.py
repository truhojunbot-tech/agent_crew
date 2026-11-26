from agent_crew.queue import TaskQueue


def test_implement_fixture_does_not_invent_agent(tmp_path):
    from agent_crew.protocol import TaskRequest
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("triage", "implement", "work", context={"risk_tier": 1}))
    assert "implementer_agent" not in queue.get_task("triage").context
