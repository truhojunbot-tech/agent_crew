"""#868: an interactive cloud CLI dialog is a terminal launch error."""

import subprocess
from types import SimpleNamespace

import pytest

from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


@pytest.mark.parametrize("finished", [False, True])
def test_interactive_prompt_fails_once_with_notice(tmp_path, monkeypatch, finished):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(
        task_id="review-868", task_type="review", description="Review #868",
        branch="fix/868", project="agent_crew",
        context={"repo": "owner/repo", "pr_number": 868,
                 "reviewed_sha": "a" * 40},
    ))
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setattr(cloud, "_validated_cloud_checkout",
                        lambda project, repo, state_path=None: ("/fixture/claude", repo))
    monkeypatch.setattr(cloud, "read_cloud_credit_used", lambda: None)
    monkeypatch.setattr(cloud, "_resolve_resume_session_id",
                        lambda queue, task: None)
    from agent_crew import server
    notices = []
    monkeypatch.setattr(server, "coordinator_notice_once",
                        lambda *args, **kwargs: notices.append((args, kwargs)))
    launches = []
    dialog = "\x1b[31mSettings Warning\x1b[0m\nInvalid entry was ignored\nEnter to confirm"

    def run(argv, **kwargs):
        if "--help" in argv:
            return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
        launches.append(argv)
        if finished:
            return SimpleNamespace(returncode=0, stdout=dialog)
        raise subprocess.TimeoutExpired(argv, 90, output=dialog.encode())

    outcome = cloud.dispatch_cloud_for_role(
        queue, role="reviewer", task_type="review", run_fn=run)

    assert not outcome.dispatched and not outcome.retryable
    assert outcome.launch_error == "cloud_launch_interactive_prompt"
    assert len(launches) == 1
    task = queue.get_task("review-868")
    assert task.status == "failed"
    assert task.error_info["reason"] == "cloud_launch_interactive_prompt"
    assert task.error_info["retryable"] is False
    assert "Settings Warning" in task.error_info["prompt_text"]
    assert "\x1b" not in task.error_info["prompt_text"]
    assert len(task.error_info["prompt_text"]) <= 300
    assert len(notices) == 1
    assert notices[0][0][2] == "cloud_launch_interactive_prompt"
