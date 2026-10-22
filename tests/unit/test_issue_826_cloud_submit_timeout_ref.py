"""#826: cloud submit timeout and fresh implement ref regressions."""

import subprocess
from types import SimpleNamespace

from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


def _queue(tmp_path, *, context=None):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(
        task_id="impl-826", task_type="implement", description="Implement #826",
        branch="fix/826-test", project="agent_crew",
        context={"repo": "owner/repo", **(context or {})},
    ))
    return queue


def _setup(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setattr(cloud, "_validated_cloud_checkout",
                        lambda project, repo, state_path=None: ("/fixture/claude", repo))
    monkeypatch.setattr(cloud, "read_cloud_credit_used", lambda: None)
    monkeypatch.setattr(cloud, "_detached_cloud_ref", lambda cwd: "b" * 40)


def test_new_implement_uses_prepared_base_not_detached_head(tmp_path, monkeypatch):
    _setup(monkeypatch)
    queue = _queue(tmp_path, context={"worktree_base_sha": "a" * 40,
                                      "base_branch": "main"})
    launches = []

    def run(argv, **kwargs):
        if "--help" in argv:
            return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
        launches.append(argv)
        return SimpleNamespace(returncode=0, stdout=(
            '{"session_id":"session_826abc",'
            '"url":"https://claude.ai/code/session_826abc"}'))

    assert cloud.dispatch_cloud_for_role(
        queue, role="implementer", task_type="implement", run_fn=run).dispatched
    assert len(launches) == 1
    assert "--ref " + "a" * 40 in launches[0][2]
    assert "--ref " + "b" * 40 not in launches[0][2]


def test_submit_timeout_retries_then_records_retryable_failure_and_notice(
        tmp_path, monkeypatch):
    _setup(monkeypatch)
    queue = _queue(tmp_path)
    attempts, notices, sleeps = [], [], []
    monkeypatch.setattr(cloud.time, "sleep", lambda seconds: sleeps.append(seconds))
    from agent_crew import server
    monkeypatch.setattr(server, "coordinator_notice_once",
                        lambda *args, **kwargs: notices.append((args, kwargs)))

    def run(argv, **kwargs):
        if "--help" in argv:
            return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
        attempts.append(argv)
        raise subprocess.TimeoutExpired(argv, 90)

    outcome = cloud.dispatch_cloud_for_role(
        queue, role="implementer", task_type="implement", run_fn=run)
    assert not outcome.dispatched and outcome.retryable
    assert len(attempts) == 3 and len(sleeps) == 2
    task = queue.get_task("impl-826")
    assert task.status == "failed"
    assert task.error_info["reason"] == "cloud_submit_timeout"
    assert task.error_info["retryable"] is True
    assert len(notices) == 1
    assert notices[0][0][2] == "cloud_submit_timeout"


def test_transient_submit_timeout_succeeds_without_notice(tmp_path, monkeypatch):
    _setup(monkeypatch)
    queue = _queue(tmp_path)
    attempts, notices = [], []
    monkeypatch.setattr(cloud.time, "sleep", lambda seconds: None)
    from agent_crew import server
    monkeypatch.setattr(server, "coordinator_notice_once",
                        lambda *args, **kwargs: notices.append(args))

    def run(argv, **kwargs):
        if "--help" in argv:
            return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
        attempts.append(argv)
        if len(attempts) == 1:
            raise subprocess.TimeoutExpired(argv, 90)
        return SimpleNamespace(returncode=0, stdout=(
            '{"session_id":"session_826abc",'
            '"url":"https://claude.ai/code/session_826abc"}'))

    outcome = cloud.dispatch_cloud_for_role(
        queue, role="implementer", task_type="implement", run_fn=run)
    assert outcome.dispatched and len(attempts) == 2
    assert notices == []


def test_fix_round_keeps_lineage_pr_head_pin(tmp_path, monkeypatch):
    _setup(monkeypatch)
    task = TaskRequest(task_id="fix-826", task_type="implement", description="Fix",
                       branch="fix/826-test", context={"prev_task_id": "impl-826",
                                                        "worktree_base_sha": "a" * 40})
    assert cloud._launch_ref(task, "/fixture/claude", "c" * 40) == "c" * 40


def test_new_implement_without_prepared_sha_uses_bare_base_branch(monkeypatch):
    _setup(monkeypatch)
    task = TaskRequest(task_id="impl-no-base", task_type="implement", description="New",
                       branch="fix/new", context={"base_branch": "release/next"})
    assert cloud._launch_ref(task, "/fixture/claude", None) == "release/next"
    assert cloud._launch_ref(TaskRequest(
        task_id="impl-invalid-base", task_type="implement", description="New",
        branch="fix/new", context={"base_branch": "../../other"}),
        "/fixture/claude", None) == ""


def test_dispatch_without_prepared_sha_pins_bare_main(tmp_path, monkeypatch):
    _setup(monkeypatch)
    queue = _queue(tmp_path)
    launches = []

    def run(argv, **kwargs):
        if "--help" in argv:
            return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
        launches.append(argv)
        return SimpleNamespace(returncode=0, stdout=(
            '{"session_id":"session_826abc",'
            '"url":"https://claude.ai/code/session_826abc"}'))

    assert cloud.dispatch_cloud_for_role(
        queue, role="implementer", task_type="implement", run_fn=run).dispatched
    assert "--ref main" in launches[0][2]
    assert "--ref origin/main" not in launches[0][2]
    assert "--ref " + "b" * 40 not in launches[0][2]


def test_timeout_with_session_output_is_recovered_without_resubmit(tmp_path, monkeypatch):
    _setup(monkeypatch)
    queue = _queue(tmp_path)
    attempts = []

    def run(argv, **kwargs):
        if "--help" in argv:
            return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
        attempts.append(argv)
        raise subprocess.TimeoutExpired(argv, 90, output=(
            b'{"session_id":"session_826abc",'
            b'"url":"https://claude.ai/code/session_826abc"}'))

    outcome = cloud.dispatch_cloud_for_role(
        queue, role="implementer", task_type="implement", run_fn=run)
    assert outcome.dispatched and len(attempts) == 1
    assert queue.get_task_context("impl-826")["cloud_session_id"] == "session_826abc"


def test_timeout_attempts_share_one_dispatch_deadline(tmp_path, monkeypatch):
    _setup(monkeypatch)
    queue = _queue(tmp_path)
    clock, timeouts = [1000.0], []
    monkeypatch.setattr(cloud.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cloud.time, "sleep", lambda seconds: None)

    def run(argv, **kwargs):
        if "--help" in argv:
            return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
        timeouts.append(kwargs.get("timeout"))
        clock[0] += 60
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 90))

    outcome = cloud.dispatch_cloud_for_role(
        queue, role="implementer", task_type="implement", run_fn=run)
    assert outcome.retryable
    assert timeouts == [90, 60]
    assert len(queue.get_task_context("impl-826")["cloud_submit_timeout_attempts"]) == 2


def test_default_submit_timeout_is_configurable(monkeypatch):
    seen = []
    monkeypatch.setenv("AGENT_CREW_CLOUD_SUBMIT_TIMEOUT_SECONDS", "120")
    monkeypatch.setattr(cloud.subprocess, "run", lambda argv, **kwargs:
                        seen.append(kwargs) or SimpleNamespace(returncode=0, stdout=""))
    cloud._default_run(["claude", "--cloud"])
    assert seen[0]["timeout"] == 120
