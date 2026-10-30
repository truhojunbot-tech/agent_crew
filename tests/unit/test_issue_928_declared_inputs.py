"""#928: routing decisions come from declarations, never prose."""

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from agent_crew.claude_cloud import _cloud_preflight_reason
from agent_crew.cli import crew
from agent_crew.pipeline import auto_enqueue_review
from agent_crew.protocol import TaskRequest
from agent_crew.queue import InvalidRiskTierError, TaskQueue, _CEA_SYSTEM_SUCCESSOR_PROVENANCE
from agent_crew.risk_tier import classify_task, risk_declaration
from agent_crew.server import create_app


@pytest.mark.parametrize("tier", range(4))
def test_description_words_cannot_change_declared_tier_or_risk(tier):
    context = {"risk_tier": tier}
    plain = "edit requested files"
    charged = "merge delete deploy server core stop queue"
    assert classify_task(plain, context) == classify_task(charged, context) == tier
    assert risk_declaration(plain, context) == risk_declaration(charged, context)


def test_missing_tier_refused_by_cli_queue_producer(tmp_db):
    queue = TaskQueue(tmp_db)
    with pytest.raises(InvalidRiskTierError, match="risk_tier"):
        queue.enqueue(TaskRequest("no-tier-928", "implement", "deploy", context={}),
                      ingress="cli.enqueue")
    assert queue.get_task("no-tier-928") is None


def test_inherited_declaration_is_not_escalated_by_review_prose():
    context = {"risk_tier": 1, "risk_declaration": {
        "safety_or_live_change": False, "human_gate_required": False,
        "inherited_from": "root-928"}}
    assert risk_declaration("merge delete deploy server core stop", context) == \
        risk_declaration("edit requested files", context)


def test_cloud_eligibility_uses_only_declared_local_inputs():
    description = "Read ~/private/secret.txt and /tmp/input.txt"
    task = TaskRequest("cloud-928", "implement", description,
                       branch="agent/cloud-928", context={"risk_tier": 1})
    assert _cloud_preflight_reason(task, "owner/repo") == ""
    task.context["local_inputs"] = ["~/private/secret.txt"]
    assert _cloud_preflight_reason(task, "owner/repo") == "cloud_local_files_required"


@pytest.mark.parametrize("local_inputs", ["/tmp/file", [1], [None], {"path": "x"}])
def test_local_inputs_must_be_list_of_strings_on_http(tmp_path, local_inputs):
    db = str(tmp_path / "tasks.db")
    app = create_app(db, pane_map={}, project="agent_crew", push_fn=lambda *a, **k: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks", json={
            "task_id": "inputs-928", "task_type": "implement", "description": "work",
            "context": {"risk_tier": 1, "local_inputs": local_inputs}},
            headers={"X-Agent-Crew-Project": "agent_crew"})
    assert response.status_code == 422
    assert "context.local_inputs" in response.text
    assert TaskQueue(db).get_task("inputs-928") is None


def test_http_accepts_declared_local_inputs(tmp_path):
    db = str(tmp_path / "tasks.db")
    app = create_app(db, pane_map={}, project="agent_crew", push_fn=lambda *a, **k: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        response = client.post("/tasks", json={
            "task_id": "inputs-928", "task_type": "implement", "description": "work",
            "context": {"risk_tier": 1, "local_inputs": ["/tmp/input"]}},
            headers={"X-Agent-Crew-Project": "agent_crew"})
    assert response.status_code == 201, response.text
    assert TaskQueue(db).get_task_context("inputs-928")["local_inputs"] == ["/tmp/input"]


def test_generated_successor_inherits_tier_without_prose_escalation(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest("parent-928", "implement", "work",
                              context={"risk_tier": 0}), ingress="loop.implement")
    queue.enqueue(TaskRequest("child-928", "review", "deploy delete server",
                              context={"prev_task_id": "parent-928"}),
                  ingress="cascade.review",
                  _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
    assert queue.get_task_context("child-928")["risk_tier"] == 0


def test_orphan_generated_successor_without_tier_is_refused(tmp_db):
    queue = TaskQueue(tmp_db)
    with pytest.raises(InvalidRiskTierError, match="risk_tier"):
        queue.enqueue(TaskRequest("orphan-928", "review", "work",
                                  context={"prev_task_id": "missing-928"}),
                      ingress="cascade.review",
                      _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
    assert queue.get_task("orphan-928") is None


def test_cli_refuses_missing_tier(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_DEFAULT_RISK_TIER", raising=False)
    result = CliRunner().invoke(crew, ["run", "deploy server", "--db",
                                       str(tmp_path / "tasks.db")])
    assert result.exit_code != 0
    assert "--risk-tier" in result.output


@pytest.mark.parametrize("tier,review_mode", [(1, None), (2, "adversarial")])
def test_review_route_is_invariant_to_description(tmp_path, monkeypatch, tier, review_mode):
    monkeypatch.setenv("AGENT_CREW_RISK_TIER_ENFORCEMENT", "1")
    for index, description in enumerate(("edit files", "merge delete deploy server core stop")):
        queue = TaskQueue(str(tmp_path / f"route-{index}.db"))
        queue.enqueue(TaskRequest(f"route-{index}", "implement", description,
                                  branch="feature", context={"risk_tier": tier}),
                      ingress="loop.implement")
        review_id = auto_enqueue_review(queue, f"route-{index}", pr_number=928,
                                        pr_state_fn=lambda *_a, **_k: "open")
        assert review_id is not None
        assert queue.get_task_context(review_id).get("review_mode") == review_mode
