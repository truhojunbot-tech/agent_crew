"""The CLI and HTTP cascade share one fix; server merge needs review status."""

import json
from unittest.mock import Mock

from click.testing import CliRunner

from agent_crew.cli import crew
from agent_crew.protocol import TaskResult
from agent_crew.queue import TaskQueue


def test_crew_run_adopts_server_fix_after_request_changes(tmp_db, monkeypatch):
    """One review verdict produces exactly one implement successor."""
    from agent_crew import pipeline

    original = TaskQueue.get_result
    seen = set()

    def result_with_server_cascade(queue, task_id):
        if task_id.startswith("fix-"):
            raise RuntimeError(f"adopted {task_id}")
        stored = original(queue, task_id)
        if stored is not None:
            return stored
        task = next(t for t in queue.list_tasks() if t.task_id == task_id)
        if task.task_type == "implement":
            queue.submit_result(task_id, TaskResult(
                task_id=task_id, status="completed", summary="implemented"))
        elif task.task_type == "review" and task_id not in seen:
            seen.add(task_id)
            queue.submit_result(task_id, TaskResult(
                task_id=task_id, status="completed", summary="fix edge case",
                verdict="request_changes", findings=["cover empty input"]))
            pipeline.auto_enqueue_fix(queue, task_id)
        return original(queue, task_id)

    monkeypatch.setattr(TaskQueue, "get_result", result_with_server_cascade)
    result = CliRunner().invoke(crew, ["run", "implement", "--db", tmp_db,
                                        "--max-iter", "2", "--timeout", "2"])
    assert isinstance(result.exception, RuntimeError), result.output
    tasks = TaskQueue(tmp_db).list_tasks()
    reviews = [t for t in tasks if t.task_type == "review"]
    assert len(reviews) == 1
    fixes = [t for t in tasks if t.task_type == "implement"
             and t.context.get("prev_task_id") == reviews[0].task_id]
    assert len(fixes) == 1
    assert str(result.exception) == f"adopted {fixes[0].task_id}"


def test_independent_status_must_succeed_on_pr_head(monkeypatch):
    from agent_crew import github

    sha = "a" * 40
    monkeypatch.setattr(github, "check_gh_installed", lambda: True)
    responses = [Mock(returncode=0, stdout=json.dumps({"headRefOid": sha})),
                 Mock(returncode=0, stdout=json.dumps({"statuses": [
                     {"context": "crew/independent-review", "state": "success"}]}))]
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return responses.pop(0)

    monkeypatch.setattr(github.subprocess, "run", run)
    assert github.independent_review_succeeded(51, "owner/repo") is True
    assert calls[1][-1] == f"repos/owner/repo/commits/{sha}/status"

    for statuses in ([], [{"context": "crew/independent-review", "state": "pending"}],
                     [{"context": "other", "state": "success"}]):
        responses[:] = [Mock(returncode=0, stdout=json.dumps({"headRefOid": sha})),
                        Mock(returncode=0, stdout=json.dumps({"statuses": statuses}))]
        assert github.independent_review_succeeded(51, "owner/repo") is False
