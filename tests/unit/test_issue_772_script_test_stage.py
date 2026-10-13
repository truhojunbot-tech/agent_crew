"""#772: the test stage is a deterministic script run with no provider call."""

import asyncio
import json
import subprocess
import sys
import urllib.error
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from agent_crew import script_tester
from agent_crew.protocol import TaskRequest
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


def test_collection_abort_shared_with_base_is_not_a_pass(project, tmp_path):
    broken = {"tests/test_a.py": "def test_one():\n    assert True\n",
              "tests/test_bad.py": "import does_not_exist\n"}
    repo, head = project(broken, {"tests/test_b.py": "def test_two():\n    assert True\n"})
    result = _execute(repo, head, tmp_path)
    record = result.error_info["script_test"]
    assert record["pre_existing"] == ["tests/test_bad.py"]
    assert result.status == "failed" and record["reason"] == "exit_code_2"


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


def test_gemini_is_not_the_default_tester():
    assert DEFAULT_ROLE_TO_AGENT["tester"] == script_tester.SCRIPT_TESTER


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
