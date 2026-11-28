"""System review admission parks legacy lineages without inventing identity."""
import pytest

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue, _CEA_SYSTEM_SUCCESSOR_PROVENANCE


@pytest.mark.parametrize("identity", ["attribution", "dispatch", "context", "absent"])
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
    if identity == "dispatch":
        queue.record_dispatch("impl", channel="api", agent="claude", target="test")
    queue.enqueue(TaskRequest("child", "review", "review",
                              context={"prev_task_id": "impl"}),
                  ingress="cascade.review",
                  _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
    saved = queue.get_task("child").context
    if identity in ("absent", "context"):
        assert "implementer_agent" not in saved
        assert queue.get_task("child").status == "blocked"
        assert saved["review_identity_blocker"]["missing_keys"] == ["implementer_agent"]
        assert saved["review_identity_blocker"]["blocker_type"] == "implementer_unknown"
    else:
        assert saved["implementer_agent"] == "claude"


def test_cascade_ingress_alone_does_not_waive_review_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    with pytest.raises(ValueError, match="implementer_agent"):
        queue.enqueue(TaskRequest("child", "review", "review",
                                  context={"risk_tier": 1, "prev_task_id": "impl"}),
                      ingress="cascade.review")


@pytest.mark.parametrize("pane_map", [{}, {"implementer": "%1", "codex": "%1"}])
def test_unknown_cascade_parks_without_role_fallback(tmp_path, monkeypatch, pane_map):
    from agent_crew.pipeline import auto_enqueue_review
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("impl", "implement", "work", branch="agent/work",
                              context={"risk_tier": 1}))
    child = auto_enqueue_review(queue, "impl", 1058, pane_map=pane_map,
                                implementer_provider="codex",
                                pr_state_fn=lambda _: "open")
    assert child == "review-impl-r0"
    assert queue.get_task(child).status == "blocked"
    assert queue.get_task(child).context["review_identity_blocker"]["missing_keys"] == [
        "implementer_agent"]
    assert queue.dequeue(role="reviewer") is None


@pytest.mark.parametrize("link", ["prev_task_id", "lineage_root_task_id", "pr"])
def test_recorded_recovery_preserves_independence(tmp_path, monkeypatch, link):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("impl", "implement", "work",
                              context={"risk_tier": 1, "pr_number": 1058}))
    queue.record_attribution("impl", agent="codex", role="implementer")
    context = {"risk_tier": 1, "prev_task_id": "legacy",
               "agent_override": "codex", "reviewed_sha": "a" * 40}
    if link == "pr":
        context["pr_number"] = 1058
    else:
        context[link] = "impl"
    queue.enqueue(TaskRequest("child", "review", "review", context=context),
                  ingress="cascade.review",
                  _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
    assert queue.get_task("child").context["implementer_agent"] == "codex"
    outcome = queue.bind_independent_reviews("codex", ["codex"])
    assert outcome["blocked"] == ["child"]
    assert queue.get_task("child").status == "blocked"
    assert queue.dequeue(role="reviewer") is None
