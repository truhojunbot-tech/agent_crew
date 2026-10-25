"""#848: the first task-less work-intent bind adopts declared task metadata."""

import json
import sqlite3
from dataclasses import asdict

import pytest

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


def _intent(**changes):
    return {
        "external_operation_id": "directive-44:execution-1",
        "project": "agent_crew", "repo": "owner/agent_crew",
        "issue": 44, "risk_tier": 3,
        "coordinator_identity": "owner", "coordinator_session": "session-1",
        "priority": 1, "execution_slo_s": 60,
        **changes,
    }


def _enqueue(queue, task_id, *, project="agent_crew", context=None):
    queue.enqueue(TaskRequest(
        task_id=task_id, task_type="implement", description="Do the work",
        branch="agent/issue-848", project=project, context=context or {},
    ))


def test_first_bind_adopts_issue_and_tier_and_records_provenance(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.admit_work_intent(_intent())
    _enqueue(queue, "impl-405", context={"repo": "owner/agent_crew",
                                         "issue": 405, "risk_tier": 1})

    bound = queue.bind_work_intent("directive-44:execution-1", "impl-405")

    assert (bound["state"], bound["task_ids"], bound["issue"], bound["risk_tier"]) == (
        "BOUND", ["impl-405"], 405, 1)
    assert queue.get_task("impl-405").context["issue"] == 405
    assert queue.get_task("impl-405").context["risk_tier"] == 1
    with sqlite3.connect(queue.db_path) as db:
        rows = db.execute(
            "SELECT fields FROM task_exec_events WHERE task_id=? AND event=?",
            ("work-intent:directive-44:execution-1", "work_intent_retargeted"),
        ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][0]) == {
        "from_issue": 44, "to_issue": 405,
        "from_risk_tier": 3, "to_risk_tier": 1,
    }


def test_second_bind_keeps_strict_issue_check(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.admit_work_intent(_intent())
    _enqueue(queue, "impl-405", context={"issue": 405, "risk_tier": 1})
    queue.bind_work_intent("directive-44:execution-1", "impl-405")
    _enqueue(queue, "impl-406", context={"issue": 406, "risk_tier": 1})

    with pytest.raises(ValueError, match="task issue differs from work intent"):
        queue.bind_work_intent("directive-44:execution-1", "impl-406")
    assert queue.get_work_intent("directive-44:execution-1")["task_ids"] == ["impl-405"]


@pytest.mark.parametrize("project, context, error", [
    ("other", {"issue": 405, "risk_tier": 1}, "project"),
    ("agent_crew", {"repo": "other/repo", "issue": 405, "risk_tier": 1}, "repo"),
])
def test_first_bind_keeps_project_and_repo_checks(tmp_path, project, context, error):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.admit_work_intent(_intent())
    _enqueue(queue, "impl-405", project=project, context=context)

    with pytest.raises(ValueError, match=f"task {error} differs from work intent"):
        queue.bind_work_intent("directive-44:execution-1", "impl-405")
    assert queue.get_work_intent("directive-44:execution-1")["task_ids"] == []


def test_first_bind_without_declared_issue_or_tier_inherits_intent(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.admit_work_intent(_intent())
    _enqueue(queue, "impl-no-fields")

    bound = queue.bind_work_intent("directive-44:execution-1", "impl-no-fields")

    assert (bound["issue"], bound["risk_tier"]) == (44, 3)
    assert queue.get_task("impl-no-fields").context["issue"] == 44
    assert queue.get_task("impl-no-fields").context["risk_tier"] == 3


@pytest.mark.parametrize("declared, expected_issue, expected_tier", [
    ({"issue": 405}, 405, 3),
    ({"risk_tier": 1}, 44, 1),
])
def test_first_bind_adopts_only_declared_field(tmp_path, declared,
                                               expected_issue, expected_tier):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.admit_work_intent(_intent())
    _enqueue(queue, "impl-partial", context=declared)

    bound = queue.bind_work_intent("directive-44:execution-1", "impl-partial")

    assert (bound["issue"], bound["risk_tier"]) == (expected_issue, expected_tier)
    assert queue.get_task("impl-partial").context["issue"] == expected_issue
    assert queue.get_task("impl-partial").context["risk_tier"] == expected_tier


def test_intent_admitted_with_task_remains_strict(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    initial = TaskRequest(
        task_id="impl-initial", task_type="implement", description="Do the work",
        branch="agent/issue-848", project="agent_crew",
        context={"issue": 44, "risk_tier": 3},
    )
    queue.admit_work_intent(_intent(task=asdict(initial)))
    _enqueue(queue, "impl-405", context={"issue": 405, "risk_tier": 1})

    with pytest.raises(ValueError, match="task issue differs from work intent"):
        queue.bind_work_intent("directive-44:execution-1", "impl-405")
    assert queue.get_work_intent("directive-44:execution-1")["task_ids"] == ["impl-initial"]


@pytest.mark.parametrize("context, field", [
    ({"issue": True}, "issue"),
    ({"issue": 0}, "issue"),
    ({"issue": "405"}, "issue"),
    ({"risk_tier": True}, "risk_tier"),
    ({"risk_tier": 4}, "risk_tier"),
    ({"risk_tier": "1"}, "risk_tier"),
])
def test_first_bind_rejects_invalid_declared_fields(tmp_path, context, field):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.admit_work_intent(_intent())
    _enqueue(queue, "impl-invalid")
    # Bind also guards legacy task rows, which may predate enqueue validation.
    with sqlite3.connect(queue.db_path) as db:
        db.execute("UPDATE tasks SET context=? WHERE task_id='impl-invalid'",
                   (json.dumps(context),))

    with pytest.raises(ValueError, match=f"task {field} differs from work intent"):
        queue.bind_work_intent("directive-44:execution-1", "impl-invalid")
    assert queue.get_work_intent("directive-44:execution-1")["task_ids"] == []
