"""#924: general test work is script-owned and gated by GitHub CI."""

import pytest
import subprocess

from agent_crew import cli, fallback, script_tester
from agent_crew.cea.engine import DEFAULT_ROLE_AGENTS
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.role_mapping import (
    effective_role_mapping, role_policy_refusal, validate_explicit_role_agents,
)


def test_general_defaults_assign_no_gemini_test_work():
    assert cli._DEFAULT_AGENTS == "codex,claude"
    assert DEFAULT_ROLE_AGENTS["tester"] == "script"
    assert effective_role_mapping(None)[0]["tester"] == "script"
    assert all("gemini" not in chain and "agy" not in chain
               for chain in fallback.DEFAULT_CHAINS.values())


@pytest.mark.parametrize("agent", ["gemini", "agy", "gemini-pro"])
def test_all_test_role_config_paths_refuse_gemini_family(agent, tmp_path):
    assert role_policy_refusal("tester", agent)
    with pytest.raises(ValueError):
        validate_explicit_role_agents({"implementer": "codex", "reviewer": "claude",
                                       "tester": agent})
    legacy, _ = effective_role_mapping({"roles": [{"role": "tester", "agent": agent}]})
    assert legacy["tester"] == "script"
    path = tmp_path / "fallback_chains.json"
    path.write_text('{"test": ["gemini", "agy", "script"]}')
    assert fallback.load_fallback_chains(str(tmp_path / "state.json"))["test"] == ["script"]


@pytest.mark.parametrize("ci_state,expected_status", [
    ("green", "completed"), ("red", "failed"), ("pending", "needs_human"),
    ("none", "needs_human"), ("error", "needs_human"),
])
def test_script_tester_folds_ci_when_workflows_exist(monkeypatch, ci_state, expected_status):
    monkeypatch.setattr(script_tester, "resolve_refs", lambda *a, **k: ("base", "a" * 40))
    monkeypatch.setattr(script_tester, "run_script_test", lambda *a, **k: {
        "outcome": script_tester.PASSED, "counts": {"passed": 2}})
    monkeypatch.setattr(script_tester, "head_has_workflows", lambda *a: True)
    calls = []
    monkeypatch.setattr("agent_crew.github.head_checks_state",
                        lambda repo, sha: calls.append((repo, sha)) or (ci_state, "CI detail"))
    task = TaskRequest(task_id="test-924", task_type="test", description="test",
                       branch="feature", context={"repo": "owner/repo", "base_branch": "main"})
    result = script_tester.execute_test_task(task, repo="/checkout", state=None,
                                              cache_dir="/cache")
    assert result.status == expected_status
    assert calls == [("owner/repo", "a" * 40)]


def test_script_tester_without_workflows_keeps_local_verdict(monkeypatch):
    monkeypatch.setattr(script_tester, "resolve_refs", lambda *a, **k: ("base", "a" * 40))
    monkeypatch.setattr(script_tester, "run_script_test", lambda *a, **k: {
        "outcome": script_tester.PASSED, "counts": {"passed": 2}})
    monkeypatch.setattr(script_tester, "head_has_workflows", lambda *a: False)
    monkeypatch.setattr("agent_crew.github.head_checks_state",
                        lambda *a: pytest.fail("CI lookup with no workflows"))
    task = TaskRequest(task_id="test-924", task_type="test", description="test",
                       branch="feature", context={"base_branch": "main"})
    result = script_tester.execute_test_task(task, repo="/checkout", state=None,
                                              cache_dir="/cache")
    assert result.status == "completed"


def test_workflow_detection_reads_tested_commit(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "README.md").write_text("base")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    base = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: CI\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "ci"], check=True)
    head = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    assert script_tester.head_has_workflows(str(tmp_path), base) is False
    assert script_tester.head_has_workflows(str(tmp_path), head) is True


def test_custom_fallback_cannot_pick_gemini_for_test():
    assert fallback.next_agent("test", None, [], {"test": ["gemini", "agy", "script"]}) == "script"


@pytest.mark.parametrize("tester", ["gemini", "agy", "claude"])
def test_refused_explicit_tester_refuses_entire_mapping(tester):
    state = {
        "role_agents": {"implementer": "claude_cloud", "reviewer": "codex",
                        "tester": tester},
        "roles": [{"role": "reviewer", "agent": "claude"}],
    }

    with pytest.raises(ValueError, match="invalid_role_agents"):
        effective_role_mapping(state, project="demo")


def test_unmapped_gemini_override_claims_review_only(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(task_id="old-implement", task_type="implement",
                              description="old", branch="main",
                              context={"agent_override": "gemini", "risk_tier": 1}))
    queue.enqueue(TaskRequest(task_id="review", task_type="review",
                              description="review", branch="main",
                              context={"agent_override": "gemini", "risk_tier": 1}))

    claimed = queue.dequeue(agent="gemini", override_task_type="review")

    assert claimed is not None and claimed.task_id == "review"
    assert queue.get_task("old-implement").status == "pending"
