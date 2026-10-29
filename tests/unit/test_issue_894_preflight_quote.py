"""#894: cloud preflight names what matched and ignores policy prose."""

import pytest

from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from tests.unit.test_issue_762_cloud_repo_resolve import _cloud_env, _run


def _task(description):
    return TaskRequest("impl-894", "implement", description, branch="agent/894-work",
                       project="agent_crew", context={"repo": "owner/agent_crew"})


@pytest.mark.parametrize("description", [
    "Fix the parser. Don't write to ~/.local/bin.",
    "Implement #894.\n- Never touch /tmp/ or ~/.agent_crew/ state",
    "Do not read /home/truhojun/alfred/secrets.md; everything is in the repo.",
    "Fix it and avoid ~/ paths entirely.",
    "Work without ~/alfred/plan.md.",
    "Do not read or write ~/.agent_crew/state.json",
    "Do not ever write anything to ~/.local/bin",
    # Review of PR #918 round 3: a negated heading scopes its bullets.
    "Do not edit these files:\n- ~/.agent_crew/state.json",
    "Fix the parser.\nNever touch these paths:\n\n- ~/.local/bin\n* /tmp/crew.lock",
])
def test_policy_prose_is_not_a_local_file_requirement(description):
    assert cloud._cloud_preflight_reason(_task(description), "owner/agent_crew") == ""


@pytest.mark.parametrize("description", [
    "Read ~/alfred/governance/plan.md before editing",
    "Don't guess. Read ~/alfred/plan.md first.",
    "Read ~/alfred/plan.md, but do not edit it.",
    # Review of PR #918: a negation that does not govern the path's access.
    "Don't guess, read ~/alfred/plan.md before editing",
    "Do not skip the input file: ~/alfred/plan.md",
    "Never guess and read ~/alfred/plan.md first",
    # Review of PR #918 round 2: the negation governs a different action.
    "Do not edit the repository but read ~/alfred/plan.md first",
    "Do not use remote API but read ~/alfred/plan.md",
    "Do not edit the repository and read ~/alfred/plan.md",
    "Never read ~/alfred/old.md but ~/alfred/plan.md is required",
    # Review of PR #918 round 3: affirmative lists stay required, and a
    # negated heading's scope ends at the next prose line.
    "Read these input files:\n- ~/alfred/plan.md",
    "Input files:\n1. ~/alfred/plan.md",
    "Do not skip these files:\n- ~/alfred/plan.md",
    "Do not edit these files:\n- ~/.agent_crew/state.json\nUse this file:\n- ~/alfred/plan.md",
    "Do not edit these files:\n- ~/.agent_crew/state.json\nThen read ~/alfred/plan.md",
    "Do not edit these files:\n- read ~/alfred/plan.md instead",
    "Do not forget to read ~/alfred/plan.md",
    "Don't hesitate to open ~/alfred/plan.md",
    "Never commit without reading ~/alfred/plan.md",
    "Without reading ~/alfred/plan.md you cannot finish; read it.",
])
def test_real_local_requirement_still_fails_closed(description):
    assert cloud._cloud_preflight_reason(_task(description), "owner/agent_crew") \
        == "cloud_local_files_required"


def test_failure_quotes_the_matched_path(tmp_path, monkeypatch):
    _cloud_env(monkeypatch)
    monkeypatch.setattr(cloud, "_validated_cloud_checkout",
                        lambda *a, **kw: ("/fixture/claude", "owner/agent_crew"))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(_task("Inspect ~/.agent_crew/state.json for this task"))
    outcome = cloud.dispatch_cloud_for_role(queue, role="implementer", task_type="implement",
                                            run_fn=_run)
    assert outcome.launch_error == "cloud_local_files_required"
    context = queue.get_task_context("impl-894")
    assert "'~/.agent_crew/state.json'" in context["cloud_terminal_error"]
    assert context["cloud_terminal_error"].startswith("cloud_local_files_required")
    assert "'~/.agent_crew/state.json'" in queue.get_task("impl-894").summary
