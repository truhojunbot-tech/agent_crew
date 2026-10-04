"""The pushed implementer result command must name its artifact refs (#554)."""

import pytest

from agent_crew.protocol import TaskRequest
from agent_crew.server import _format_task_message


RULE = (
    "implement results without branch+commit (or pr_number) are held as "
    "no_artifact; resend with them"
)


@pytest.mark.parametrize("nonce", [None, "test-nonce"])
def test_default_implement_result_template_includes_refs_and_rule(nonce):
    task = TaskRequest(
        task_id="impl-554", task_type="implement", description="change code",
    )
    message = _format_task_message(task, 8105, nonce=nonce, project="agent_crew")
    result = message[message.index("Do the work described above, then POST result:"):]

    assert result.index(RULE) < result.index("curl -s -X POST")
    assert '"branch":"<branch-name>"' in result
    assert '"commit":"<full-commit-sha>"' in result
    assert '"pr_number":null' in result
    if nonce:
        assert '"executor_binding":{"nonce":"test-nonce"}' in result


@pytest.mark.parametrize("artifact_kind", ["report", "rebase", "review"])
def test_other_implement_artifact_kinds_keep_their_result_template(artifact_kind):
    task = TaskRequest(
        task_id="impl-554", task_type="implement", description="work",
        context={"artifact_kind": artifact_kind},
    )
    message = _format_task_message(task, 8105)

    assert RULE not in message
    assert message.endswith(
        '-d \'{"task_id":"impl-554","status":"completed",'
        '"summary":"...","findings":[]}\''
    )


@pytest.mark.parametrize("task_type", ["review", "test"])
def test_other_role_result_templates_are_unchanged(task_type):
    task = TaskRequest(
        task_id="task-554", task_type=task_type, description="work",
    )
    message = _format_task_message(task, 8105, nonce="test-nonce")

    assert RULE not in message
    assert message.endswith(
        '-d \'{"task_id":"task-554","status":"completed",'
        '"summary":"...","findings":[],"executor_binding":'
        '{"nonce":"test-nonce"}}\''
    )
