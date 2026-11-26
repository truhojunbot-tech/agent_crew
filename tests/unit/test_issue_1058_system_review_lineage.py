"""System review admission preserves legacy lineages without inventing identity."""
import pytest

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue, _CEA_SYSTEM_SUCCESSOR_PROVENANCE


@pytest.mark.parametrize("identity", ["attribution", "context", "absent"])
def test_system_review_recovers_recorded_implementer(tmp_path, monkeypatch, identity):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    context = {"risk_tier": 1}
    if identity == "context":
        context["agent_override"] = "claude"
    queue.enqueue(TaskRequest("impl", "implement", "work", context=context),
                  ingress="loop.implement")
    if identity == "attribution":
        queue.record_attribution("impl", agent="claude", role="implementer")
    queue.enqueue(TaskRequest("child", "review", "review",
                              context={"prev_task_id": "impl"}),
                  ingress="cascade.review",
                  _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
    saved = queue.get_task("child").context
    if identity == "absent":
        assert "implementer_agent" not in saved
    else:
        assert saved["implementer_agent"] == "claude"


def test_cascade_ingress_alone_does_not_waive_review_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    with pytest.raises(ValueError, match="implementer_agent"):
        queue.enqueue(TaskRequest("child", "review", "review",
                                  context={"risk_tier": 1, "prev_task_id": "impl"}),
                      ingress="cascade.review")
