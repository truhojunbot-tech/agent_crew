"""#761: every launch_shadow_review skip logs one stable reason token."""

import json
import logging
from types import SimpleNamespace

import pytest

from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


SHA = "a" * 40
SESSION = "session_abc12345"
SKIP_PREFIX = "claude_cloud: shadow skip "


def _run(argv, **kwargs):
    if "--help" in argv:
        return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
    return SimpleNamespace(returncode=0, stdout=json.dumps({
        "session_id": SESSION, "url": f"https://claude.ai/code/{SESSION}"}))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setenv("AGENT_CREW_CLOUD_SHADOW", "1")
    monkeypatch.delenv("AGENT_CREW_CLOUD_MAX_PER_DAY", raising=False)
    monkeypatch.setattr(cloud, "_validated_cloud_checkout",
                        lambda project, repo, state_path=None: ("/fixture/claude", repo))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(
        task_id="review-761", task_type="review", description="Review pinned PR",
        branch="agent/review-761", project="agent_crew",
        context={"pr_number": 761, "reviewed_sha": SHA, "repo": "owner/repo"},
    ))
    return queue, queue.dequeue(role="reviewer")


def _skip_reasons(caplog):
    return [r.getMessage()[len(SKIP_PREFIX):] for r in caplog.records
            if r.name == cloud.logger.name and r.getMessage().startswith(SKIP_PREFIX)]


def _arrange(reason, queue, task, monkeypatch):
    if reason == "daily_cap":
        monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_PER_DAY", "0")
    elif reason == "concurrency":
        monkeypatch.setattr(cloud, "cloud_max_concurrency", lambda: 0)
    elif reason == "no_pr_sha":
        task.context = {**task.context, "reviewed_sha": "not-a-sha"}
    elif reason == "probe_unsupported":
        monkeypatch.setattr(cloud, "probe_cloud_cli",
                            lambda run: SimpleNamespace(supported=False))
    elif reason == "no_repo":
        monkeypatch.setattr(cloud, "_resolve_task_repo", lambda *args, **kwargs: None)
    elif reason == "cwd_mismatch":
        monkeypatch.setattr(cloud, "_validated_cloud_checkout",
                            lambda project, repo, state_path=None: (None, None))
    elif reason == "already_reserved":
        queue.patch_context(task.task_id, {"cloud_shadow_session_id": SESSION})
    return task


@pytest.mark.parametrize("reason", ["daily_cap", "concurrency", "no_pr_sha",
                                    "probe_unsupported", "no_repo", "cwd_mismatch",
                                    "already_reserved"])
def test_each_skip_branch_logs_its_reason_token(env, monkeypatch, caplog, reason):
    queue, task = env
    task = _arrange(reason, queue, task, monkeypatch)
    with caplog.at_level(logging.INFO, logger=cloud.logger.name):
        assert cloud.launch_shadow_review(queue, task, run_fn=_run) is False
    assert _skip_reasons(caplog) == [f"task={task.task_id} reason={reason}"]


def test_lost_reservation_race_logs_already_reserved(env, monkeypatch, caplog):
    queue, task = env
    monkeypatch.setattr(queue, "reserve_cloud_shadow_launch", lambda task_id: False)
    with caplog.at_level(logging.INFO, logger=cloud.logger.name):
        assert cloud.launch_shadow_review(queue, task, run_fn=_run) is False
    assert _skip_reasons(caplog) == [f"task={task.task_id} reason=already_reserved"]


def test_disabled_and_non_review_do_not_log_skip(env, monkeypatch, caplog):
    queue, task = env
    with caplog.at_level(logging.INFO, logger=cloud.logger.name):
        monkeypatch.setenv("AGENT_CREW_CLOUD_SHADOW", "0")
        assert cloud.launch_shadow_review(queue, task, run_fn=_run) is False
        monkeypatch.setenv("AGENT_CREW_CLOUD_SHADOW", "1")
        task.task_type = "implement"
        assert cloud.launch_shadow_review(queue, task, run_fn=_run) is False
    assert _skip_reasons(caplog) == []


def test_successful_launch_logs_no_skip(env, caplog):
    queue, task = env
    with caplog.at_level(logging.INFO, logger=cloud.logger.name):
        assert cloud.launch_shadow_review(queue, task, run_fn=_run) is True
    assert _skip_reasons(caplog) == []
