"""#772: the test stage is a deterministic script run with no provider call."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from agent_crew import script_tester
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.role_mapping import DEFAULT_ROLE_TO_AGENT
from agent_crew.server import create_app

PYTEST = f"{sys.executable} -m pytest -q -p no:cacheprovider"
PR = 7


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _commit(repo, files, message):
    for name, body in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def project(tmp_path):
    """An origin with ``main`` and a PR head at ``refs/pull/7/head``, plus a clone."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = tmp_path / "author"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True,
                   capture_output=True)
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-qb", "main")
    clone = tmp_path / "checkout"

    def build(base_files, pr_files):
        _commit(work, base_files, "base")
        _git(work, "push", "-q", "origin", "main")
        _git(work, "checkout", "-qb", "feature")
        head = _commit(work, pr_files, "pr")
        _git(work, "push", "-q", "origin", f"HEAD:refs/pull/{PR}/head")
        _git(work, "checkout", "-q", "main")
        subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True,
                       capture_output=True)
        return str(clone), head

    return build


GREEN = {"tests/test_a.py": "def test_one():\n    assert True\n"}
BROKEN_BASE = {"tests/test_a.py": "def test_one():\n    assert True\n",
               "tests/test_old.py": "def test_legacy():\n    assert False\n"}


def _task(head, **ctx):
    return TaskRequest(task_id="test-review-1", task_type="test", description="Test PR #7",
                       branch="feature", context={"pr_number": PR, "base_branch": "main",
                                                  "reviewed_sha": head, **ctx})


def _execute(repo, head, tmp_path, state=None):
    return script_tester.execute_test_task(
        _task(head), repo=repo, state={"test_command": PYTEST} if state is None else state,
        cache_dir=str(tmp_path / "cache"))


def test_green_suite_passes_with_counts(project, tmp_path):
    repo, head = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert True\n"})
    result = _execute(repo, head, tmp_path)
    record = result.error_info["script_test"]
    assert result.status == "completed"
    assert record["outcome"] == "PASSED"
    assert record["counts"] == {"passed": 2}
    assert record["head_sha"] == head and record["rebased"] is True
    assert record["provider_calls"] == 0


def test_new_failure_fails_with_test_ids(project, tmp_path):
    repo, head = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert False\n"})
    result = _execute(repo, head, tmp_path)
    record = result.error_info["script_test"]
    assert result.status == "failed"
    assert record["new_failures"] == ["tests/test_b.py::test_two"]
    assert record["pre_existing"] == []
    assert record["counts"] == {"failed": 1, "passed": 1}
    assert "new failure: tests/test_b.py::test_two" in result.findings
    assert result.error_info["reason"] == "script_test_new_failures"


def test_pre_existing_failures_only_pass_with_list_and_cache(project, tmp_path):
    repo, head = project(BROKEN_BASE, {"tests/test_b.py": "def test_two():\n    assert True\n"})
    result = _execute(repo, head, tmp_path)
    record = result.error_info["script_test"]
    assert result.status == "completed"
    assert record["outcome"] == "PASSED"
    assert record["pre_existing"] == ["tests/test_old.py::test_legacy"]
    assert record["new_failures"] == []
    assert record["base"]["cached"] is False
    again = _execute(repo, head, tmp_path).error_info["script_test"]
    assert again["base"]["cached"] is True
    assert again["pre_existing"] == record["pre_existing"]


def test_spaced_parametrized_id_is_not_mistaken_for_pre_existing(project, tmp_path):
    base = {"tests/test_cases.py": (
        'import pytest\n@pytest.mark.parametrize("case", ["a b", "a c"])\n'
        'def test_case(case):\n    assert case != "a b"\n')}
    head = {"tests/test_cases.py": (
        'import pytest\n@pytest.mark.parametrize("case", ["a b", "a c"])\n'
        'def test_case(case):\n    assert False\n')}
    repo, head_sha = project(base, head)
    result = _execute(repo, head_sha, tmp_path)
    record = result.error_info["script_test"]
    assert result.status == "failed"
    assert record["pre_existing"] == ["tests/test_cases.py::test_case[a b]"]
    assert record["new_failures"] == ["tests/test_cases.py::test_case[a c]"]


def test_failure_parser_keeps_separator_inside_param_id():
    text = "FAILED tests/test_x.py::test_case[a - b] - AssertionError: bad - detail\n"
    _, ids = script_tester.parse_test_output(text)
    assert ids == ["tests/test_x.py::test_case[a - b]"]


def test_collection_abort_shared_with_base_is_not_a_pass(project, tmp_path):
    broken = {"tests/test_a.py": "def test_one():\n    assert True\n",
              "tests/test_bad.py": "import does_not_exist\n"}
    repo, head = project(broken, {"tests/test_b.py": "def test_two():\n    assert True\n"})
    result = _execute(repo, head, tmp_path)
    record = result.error_info["script_test"]
    assert record["pre_existing"] == ["tests/test_bad.py"]
    assert result.status == "failed" and record["reason"] == "exit_code_2"


def test_new_collection_error_is_not_a_test_failure_verdict(project, tmp_path):
    repo, head = project(GREEN, {"tests/test_bad.py": "import does_not_exist\n"})
    result = _execute(repo, head, tmp_path)
    assert result.error_info["script_test"]["reason"] == "exit_code_2"
    from agent_crew.loop import handle_test_result
    assert handle_test_result(result) == "not_run"


def test_no_command_is_skipped_not_passed(project, tmp_path):
    repo, head = project({"README": "x\n"}, {"README": "y\n"})   # no tests/
    result = _execute(repo, head, tmp_path, state={})
    assert result.status == "needs_human"
    assert result.error_info["script_test"]["outcome"] == "SKIPPED_NO_TEST_COMMAND"
    assert result.summary.startswith("SKIPPED_NO_TEST_COMMAND")


def test_merge_conflict_fails_on_the_rebased_head(project, tmp_path):
    repo, head = project(GREEN, {"tests/test_a.py": "def test_one():\n    assert 1\n"})
    # Move main after the PR branched so the squash cannot apply cleanly.
    author = tmp_path / "author"
    _commit(author, {"tests/test_a.py": "def test_one():\n    assert 2\n"}, "main moves")
    _git(author, "push", "-q", "origin", "main")
    result = _execute(repo, head, tmp_path)
    assert result.status == "failed"
    assert result.error_info["script_test"]["reason"] == "merge_conflict"


def test_config_default_and_timeout(tmp_path):
    assert script_tester.resolve_test_config({}, str(tmp_path)) == (None, 1800.0)
    (tmp_path / "tests").mkdir()
    assert script_tester.resolve_test_config({}, str(tmp_path))[0] == "python3 -m pytest -q"
    assert script_tester.resolve_test_config(
        {"test_command": "", "test_timeout_s": 60}, str(tmp_path)) == (None, 60.0)
    assert script_tester.resolve_test_config(
        {"test_command": "make test"}, str(tmp_path))[0] == "make test"


def test_timeout_fails_the_stage(tmp_path):
    outcome = script_tester.run_command("sleep 5", str(tmp_path), 0.2)
    assert outcome.timed_out and outcome.exit_code is None


def test_test_command_isolated_from_dispatcher_and_provider_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_PROJECT", "live_project")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("PYTHONPATH", "/wrong/source")
    command = (f"{sys.executable} -c 'import os; "
               "assert \"AGENT_CREW_PROJECT\" not in os.environ; "
               "assert \"OPENAI_API_KEY\" not in os.environ; "
               "assert os.environ[\"PYTHONPATH\"] == os.path.join(os.getcwd(), \"src\"); "
               "assert os.environ[\"PROJECT_TEST_FLAG\"] == \"yes\"'")
    outcome = script_tester.run_command(
        command, str(tmp_path), 10,
        env=script_tester.test_environment(str(tmp_path),
                                           {"test_env": {"PROJECT_TEST_FLAG": "yes"}}))
    assert outcome.exit_code == 0, outcome.output_tail


def test_test_environment_rejects_runtime_and_provider_keys(tmp_path):
    for key in ("AGENT_CREW_PROJECT", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        with pytest.raises(ValueError, match=key):
            script_tester.test_environment(str(tmp_path), {"test_env": {key: "x"}})


def test_base_cache_identity_changes_with_test_environment(project, tmp_path):
    repo, head = project(BROKEN_BASE, {"tests/test_b.py": "def test_two():\n    assert True\n"})
    first = _execute(repo, head, tmp_path, state={"test_command": PYTEST,
                                                   "test_env": {"PROJECT_TEST_FLAG": "one"}})
    second = _execute(repo, head, tmp_path, state={"test_command": PYTEST,
                                                    "test_env": {"PROJECT_TEST_FLAG": "two"}})
    third = _execute(repo, head, tmp_path, state={"test_command": PYTEST,
                                                   "test_env": {"PROJECT_TEST_FLAG": "two"}})
    assert first.error_info["script_test"]["base"]["cached"] is False
    assert second.error_info["script_test"]["base"]["cached"] is False
    assert third.error_info["script_test"]["base"]["cached"] is True


def test_gemini_is_not_the_default_tester():
    assert DEFAULT_ROLE_TO_AGENT["tester"] == script_tester.SCRIPT_TESTER


@pytest.mark.parametrize("claim_source", ["dispatcher", "script_tester"])
def test_watchdog_uses_two_suite_timeouts_for_running_script_test(
        tmp_path, monkeypatch, claim_source):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"test_timeout_s": 1200}))
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest(task_id=f"test-{claim_source}", task_type="test",
                              description="script test", branch="main"))
    started_at = time.time()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE tasks SET status='in_progress', claim_source=?, "
            "last_activity_at=?, dispatched_at=?, lease_owner=?, "
            "dispatch_agent='script', dispatch_target='script_tester' WHERE task_id=?",
            (claim_source, started_at, started_at,
             "script:pid:1" if claim_source == "dispatcher" else None,
             f"test-{claim_source}"))
    queue.patch_context(f"test-{claim_source}", {"script_test_timeout_s": 1200})
    # The running suite uses the config captured at dispatch, even if the
    # project state is edited while it runs.
    state_path.write_text(json.dumps({"test_timeout_s": 1}))
    app = create_app(db, pane_map={"tester": "%3"} if claim_source == "dispatcher" else {},
                     state_path=str(state_path),
                     timeout_seconds=900, watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        early = app.state.watchdog_tick(started_at + 901)
        assert early["timed_out"] == []
        assert queue.get_task_status(f"test-{claim_source}") == "in_progress"
        late = app.state.watchdog_tick(started_at + 3001)
        assert late["timed_out"] == [f"test-{claim_source}"]


def test_watchdog_keeps_provider_test_on_dispatcher_reap_under_script_default(
        tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"test_timeout_s": 1200}))
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest(task_id="provider-test", task_type="test",
                              description="provider test", branch="main"))
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE tasks SET status='in_progress', claim_source='dispatcher', "
            "last_activity_at=1000, dispatched_at=1000, "
            "dispatch_agent='gemini', dispatch_target='pid:123' "
            "WHERE task_id='provider-test'")
    app = create_app(db, pane_map={}, state_path=str(state_path),
                     timeout_seconds=900, watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        actions = app.state.watchdog_tick(1901)
    assert actions["timed_out"] == ["provider-test"]
    assert app.state.watchdog_expiry_counts["dispatcher_idle"] == 1


# ── server: the real test-stage path, with provider calls counted ───────────

@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    pushes, spawns, merges = [], [], []

    async def no_provider(*args, **kwargs):
        spawns.append(args)
        raise AssertionError("provider subprocess spawned in the test stage")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_provider)
    import agent_crew.github as gh
    monkeypatch.setattr(gh, "merge_pr", lambda *a, **k: False)
    reserve = TaskQueue.external_op_reserve

    def spy_reserve(self, op_key, *args, **kwargs):
        merges.append(op_key)          # the auto-merge gate was entered
        return reserve(self, op_key, *args, **kwargs)

    monkeypatch.setattr(TaskQueue, "external_op_reserve", spy_reserve)
    monkeypatch.setattr(gh, "pr_head_sha", lambda *a, **k: "")
    monkeypatch.setattr(gh, "pr_state", lambda *a, **k: "open")

    def build(repo, state):
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps({"project": "proj", **state}))
        db = str(tmp_path / "tasks.db")
        app = create_app(db, pane_map={"tester": "%9"}, project="proj",
                         push_fn=lambda pane, text: pushes.append(pane),
                         watchdog_disabled=True, anomaly_disabled=True,
                         state_path=str(state_path),
                         worktree_map={"implementer": repo, "reviewer": repo})
        return app, TaskQueue(db)

    return build, pushes, spawns, merges


def _run_through_push_path(app, queue, task):
    queue.enqueue(task)
    with TestClient(app):
        app.state.drain_script_tests()
    return queue.get_task(task.task_id)


def test_server_runs_test_stage_by_script_and_merges_on_pass(project, server):
    build, pushes, spawns, merges = server
    repo, head = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert True\n"})
    app, queue = build(repo, {"test_command": PYTEST})
    done = _run_through_push_path(app, queue, _task(head))
    assert done.status == "completed"
    assert queue.get_task_context(done.task_id)["script_test"]["counts"] == {"passed": 2}
    assert queue.get_task_context(done.task_id)["script_test_timeout_s"] == 1800
    assert (pushes, spawns) == ([], [])          # 0 provider calls
    assert merges == [f"merge:pr:{PR}"]


def test_server_new_failure_fails_without_retry_or_merge(project, server):
    build, pushes, spawns, merges = server
    repo, head = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert False\n"})
    app, queue = build(repo, {"test_command": PYTEST})
    done = _run_through_push_path(app, queue, _task(head))
    assert done.status == "failed"
    assert queue.get_task_context(done.task_id)["script_test"]["new_failures"] == [
        "tests/test_b.py::test_two"]
    assert [t.task_id for t in queue.list_tasks()] == [done.task_id]   # no retry clone
    assert (pushes, spawns, merges) == ([], [], [])


def test_server_no_command_never_auto_merges(project, server):
    build, pushes, spawns, merges = server
    repo, head = project({"README": "x\n"}, {"README": "y\n"})
    app, queue = build(repo, {})
    done = _run_through_push_path(app, queue, _task(head))
    assert done.status == "needs_human"
    assert queue.get_task_context(done.task_id)["script_test"]["outcome"] == (
        "SKIPPED_NO_TEST_COMMAND")
    assert (pushes, spawns, merges) == ([], [], [])


def test_dispatcher_path_runs_script_not_provider(project, server):
    build, pushes, spawns, merges = server
    repo, head = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert True\n"})
    app, queue = build(repo, {"test_command": PYTEST})
    queue.enqueue(_task(head))
    with TestClient(app):
        task = queue.dequeue(agent="script", role="tester")
        asyncio.run(app.state.dispatch_task(task, "tester"))
    assert queue.get_task(task.task_id).status == "completed"
    assert (pushes, spawns) == ([], [])


def test_approved_review_cascades_into_a_script_test(project, server, monkeypatch, github_writes):
    build, pushes, spawns, merges = server
    repo, head = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert True\n"})
    import agent_crew.github as gh
    monkeypatch.setattr(gh, "pr_head_sha", lambda *a, **k: head)
    app, queue = build(repo, {"test_command": PYTEST})
    review = "review-772"
    queue.enqueue(TaskRequest(task_id=review, task_type="review", description="review PR",
                              branch="feature", context={"pr_number": PR, "reviewed_sha": head,
                                                         "repo": "owner/repo",
                                                         "base_branch": "main"}))
    with TestClient(app) as client:
        assert queue.dequeue(role="reviewer") is not None
        resp = client.post(f"/tasks/{review}/result", headers={"X-Agent-Crew-Project": "proj"},
                           json={"task_id": review, "status": "completed",
                                 "summary": "Approved: the change is small and covered by tests.",
                                 "verdict": "approve", "findings": []})
        assert resp.status_code == 200, resp.text
        app.state.script_test_thread.join(timeout=120)
    test = queue.get_task(f"test-{review}")
    assert test.status == "completed"
    assert queue.get_task_context(test.task_id)["script_test"]["counts"] == {"passed": 2}
    assert (pushes, spawns) == ([], [])


# ── CLI: the real `crew run` loop ───────────────────────────────────────────

COMMIT = "c" * 40


class _LoopQueue:
    def __init__(self, results):
        self.results = iter(results)

    def get_result(self, task_id):
        return next(self.results)


def _crew_run(monkeypatch, tmp_path, results, *, review_raises=None, tester=False):
    import agent_crew.loop as loop
    from agent_crew.cli import crew

    dispatched = []

    def enqueue_implement(queue, task, branch, context=None, port=0):
        dispatched.append("implement")
        return f"impl-{dispatched.count('implement')}"

    def enqueue_review(queue, task, branch, prev_task_id=None, context=None, port=0):
        dispatched.append("review")
        if review_raises is not None:
            raise review_raises
        return "review-1"

    def enqueue_test(queue, task, branch, prev_task_id="", context=None, port=0):
        dispatched.append("test")
        return "test-review-1"

    monkeypatch.setattr("agent_crew.queue.TaskQueue", lambda db: _LoopQueue(results))
    monkeypatch.setattr(loop, "enqueue_implement", enqueue_implement)
    monkeypatch.setattr(loop, "enqueue_review", enqueue_review)
    monkeypatch.setattr(loop, "enqueue_test", enqueue_test)
    args = ["run", "work", "--db", str(tmp_path / "tasks.db"), "--max-iter", "2"]
    if not tester:
        args.append("--no-tester")
    return CliRunner().invoke(crew, args), dispatched


def _impl_done():
    from agent_crew.protocol import TaskResult
    return TaskResult("impl-1", "completed", "done", branch="main", commit=COMMIT)


def test_run_loop_409_on_enqueue_review_exits_cleanly(monkeypatch, tmp_path):
    conflict = urllib.error.HTTPError("http://127.0.0.1:8101/tasks", 409, "Conflict", {}, None)
    out, dispatched = _crew_run(monkeypatch, tmp_path, [_impl_done()], review_raises=conflict)
    assert out.exception is None, out.output
    assert out.exit_code == 0
    assert "409 Conflict" in out.output and "Stopping the run loop" in out.output
    assert dispatched == ["implement", "review"]


def test_run_loop_other_http_errors_still_raise(monkeypatch, tmp_path):
    boom = urllib.error.HTTPError("http://127.0.0.1:8101/tasks", 500, "boom", {}, None)
    out, _ = _crew_run(monkeypatch, tmp_path, [_impl_done()], review_raises=boom)
    assert isinstance(out.exception, urllib.error.HTTPError)


def test_run_loop_skipped_test_stage_stops_without_reimplement(monkeypatch, tmp_path):
    from agent_crew.protocol import TaskResult

    skipped = script_tester.record_to_result(
        "test-review-1", {"outcome": "SKIPPED_NO_TEST_COMMAND"}, pr_number=PR)
    results = [_impl_done(),
               TaskResult("review-1", "completed", "ok", verdict="approve", findings=[]),
               skipped]
    out, dispatched = _crew_run(monkeypatch, tmp_path, results, tester=True)
    assert out.exception is None, out.output
    assert "SKIPPED_NO_TEST_COMMAND" in out.output
    assert "left for the coordinator gate" in out.output
    assert dispatched == ["implement", "review", "test"]


@pytest.mark.parametrize("test_result", [
    script_tester.record_to_result("test-review-1", {"outcome": "FAILED", "reason": "timeout"}),
    script_tester.record_to_result("test-review-1", {"outcome": "FAILED", "reason": "exit_code_4"}),
    script_tester.record_to_result("test-review-1", {"outcome": "FAILED", "reason": "merge_conflict"}),
    script_tester.record_to_result("test-review-1", {"outcome": "SETUP_ERROR", "detail": "config"}),
    TaskResult(
        "test-review-1", "failed", "provider exit_1", error_info={"reason": "exit_1", "final": True}),
    TaskResult(
        "test-review-1", "failed", "invalid model", error_info={"reason": "config_error"}),
])
def test_run_loop_unjudged_test_stage_stops_without_reimplement(monkeypatch, tmp_path,
                                                                 test_result):
    from agent_crew.protocol import TaskResult

    results = [_impl_done(),
               TaskResult("review-1", "completed", "ok", verdict="approve", findings=[]),
               test_result]
    out, dispatched = _crew_run(monkeypatch, tmp_path, results, tester=True)
    assert out.exception is None, out.output
    assert "test stage not_run" in out.output
    assert "coordinator gate" in out.output
    assert dispatched == ["implement", "review", "test"]
