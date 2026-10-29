"""#772: the test stage is a deterministic script run with no provider call."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
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


@pytest.mark.parametrize(("command", "node_id", "expected"), [
    (
        "python3 -m pytest -q --timeout=180 -p no:cacheprovider tests",
        "tests/unit/test_x.py::test_y",
        "python3 -m pytest -q --timeout=180 -p no:cacheprovider tests/unit/test_x.py::test_y",
    ),
    (
        "pytest --timeout 180 tests/unit",
        "tests/unit/test_x.py::test_y",
        "pytest --timeout 180 tests/unit/test_x.py::test_y",
    ),
    (
        "pytest --capture=no tests",
        "tests/unit/test_x.py::test_y",
        "pytest --capture=no tests/unit/test_x.py::test_y",
    ),
    ("pytest tests/unit_other", "tests/unit/test_x.py::test_y", None),
    ("pytest -k smoke tests", "tests/unit/test_x.py::test_y", None),
    ("pytest -k=smoke tests", "tests/unit/test_x.py::test_y", None),
    ("pytest tests && echo done", "tests/unit/test_x.py::test_y", None),
    ("make test", "tests/unit/test_x.py::test_y", None),
])
def test_isolated_rerun_command_replaces_only_matching_suite_target(command, node_id, expected):
    assert script_tester._isolated_rerun_command(command, node_id) == expected


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
                                                  "reviewed_sha": head, "repo": "owner/repo", **ctx})


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


def test_failure_parser_excludes_log_lines_and_collection_paths():
    text = ("ERROR agent_crew.server:server.py:1199 provider failed\n"
            "ERROR tests/test_bad.py - ImportError: missing\n"
            "FAILED tests/test_x.py::test_real - AssertionError: bad\n")
    _, ids = script_tester.parse_test_output(text)
    assert ids == ["tests/test_x.py::test_real"]


@pytest.mark.parametrize("rerun_exit_codes, expected_outcome, expected_flaky, expected_reruns", [
    ([0], "PASSED", ["tests/test_b.py::test_two"], 1),
    ([1, 0], "PASSED", ["tests/test_b.py::test_two"], 2),
    ([1, 1], "FAILED", [], 2),
])
def test_new_failure_is_judged_from_isolated_head_reruns(
        project, tmp_path, rerun_exit_codes, expected_outcome, expected_flaky,
        expected_reruns):
    repo, head_sha = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert False\n"})
    base_sha, _ = script_tester.resolve_refs(
        repo, base_branch="main", pr_number=PR, head_sha=head_sha, branch="feature")
    node = "tests/test_b.py::test_two"
    calls = []

    def runner(command, cwd, timeout_s, *, env=None):
        calls.append((command, cwd, dict(env)))
        if len(calls) == 1:
            return script_tester.RunOutcome(exit_code=1, failing_ids=[node],
                                            counts={"failed": 1}, duration_s=3)
        if command == PYTEST:
            return script_tester.RunOutcome(exit_code=0, duration_s=2)
        code = rerun_exit_codes.pop(0)
        return script_tester.RunOutcome(exit_code=code, duration_s=1,
                                        failing_ids=[node] if code else [])

    record = script_tester.run_script_test(
        repo, base_sha=base_sha, head_sha=head_sha,
        state={"test_command": PYTEST}, cache_dir=str(tmp_path / "cache"),
        runner=runner)
    assert record["outcome"] == expected_outcome
    assert record["flaky"] == expected_flaky
    assert record["new_failures"] == ([] if expected_flaky else [node])
    assert len(record["reruns"][node]) == expected_reruns
    rerun_calls = [call for call in calls if call[0] != PYTEST]
    assert all(command.endswith(" " + node) for command, _, _ in rerun_calls)
    assert all(cwd == calls[0][1] and env == calls[0][2]
               for _, cwd, env in rerun_calls)
    result = script_tester.record_to_result("test-flake", record)
    assert result.status == ("completed" if expected_flaky else "failed")
    if expected_flaky:
        assert node in result.summary and "flaky" in result.summary


def test_isolated_rerun_timeout_is_incomplete(project, tmp_path):
    repo, head_sha = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert False\n"})
    base_sha, _ = script_tester.resolve_refs(
        repo, base_branch="main", pr_number=PR, head_sha=head_sha, branch="feature")
    node = "tests/test_b.py::test_two"

    def runner(command, cwd, timeout_s, *, env=None):
        if command != PYTEST:
            return script_tester.RunOutcome(exit_code=None, timed_out=True, duration_s=10)
        if "test_b.py" in os.listdir(os.path.join(cwd, "tests")):
            return script_tester.RunOutcome(exit_code=1, failing_ids=[node], duration_s=1)
        return script_tester.RunOutcome(exit_code=0, duration_s=1)

    record = script_tester.run_script_test(
        repo, base_sha=base_sha, head_sha=head_sha,
        state={"test_command": PYTEST, "test_timeout_s": 10},
        cache_dir=str(tmp_path / "cache"), runner=runner)
    assert record["outcome"] == "INCOMPLETE"
    assert record["reason"] == "script_test_timeout"
    assert script_tester.record_to_result("test-rerun-timeout", record).status == "needs_human"


def test_many_new_failures_do_not_start_unbounded_reruns(project, tmp_path):
    repo, head_sha = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert False\n"})
    base_sha, _ = script_tester.resolve_refs(
        repo, base_branch="main", pr_number=PR, head_sha=head_sha, branch="feature")
    ids = [f"tests/test_b.py::test_case_{i}" for i in range(6)]
    calls = []

    def runner(command, cwd, timeout_s, *, env=None):
        calls.append(command)
        if command != PYTEST:
            pytest.fail("a large failure set must not start isolated reruns")
        if "test_b.py" in os.listdir(os.path.join(cwd, "tests")):
            return script_tester.RunOutcome(exit_code=1, failing_ids=ids)
        return script_tester.RunOutcome(exit_code=0)

    record = script_tester.run_script_test(
        repo, base_sha=base_sha, head_sha=head_sha,
        state={"test_command": PYTEST}, cache_dir=str(tmp_path / "cache"),
        runner=runner)
    assert record["outcome"] == "FAILED"
    assert record["reason"] == "new_failures"
    assert record["new_failures"] == ids
    assert record["reruns_skipped"] == "script_test_rerun_limit"
    assert record["reruns"] == {}
    result = script_tester.record_to_result("test-many-failures", record)
    assert result.status == "failed"
    assert result.findings == [f"new failure: {node}" for node in ids]
    from agent_crew.loop import handle_test_result
    assert handle_test_result(result) == "failed"
    assert calls == [PYTEST, PYTEST]


@pytest.mark.parametrize("command", ["pytest tests/unit", "make test", "pytest -q && echo done"])
def test_non_plain_command_does_not_rerun_whole_suite(project, tmp_path, command):
    repo, head_sha = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert False\n"})
    base_sha, _ = script_tester.resolve_refs(
        repo, base_branch="main", pr_number=PR, head_sha=head_sha, branch="feature")
    node = "tests/test_b.py::test_two"
    calls = []

    def runner(actual, cwd, timeout_s, *, env=None):
        calls.append(actual)
        if actual != command:
            pytest.fail("configured suite command must not be reused for a node rerun")
        if "test_b.py" in os.listdir(os.path.join(cwd, "tests")):
            return script_tester.RunOutcome(exit_code=1, failing_ids=[node])
        return script_tester.RunOutcome(exit_code=0)

    record = script_tester.run_script_test(
        repo, base_sha=base_sha, head_sha=head_sha,
        state={"test_command": command}, cache_dir=str(tmp_path / "cache"),
        runner=runner)
    assert record["outcome"] == "FAILED"
    assert record["reason"] == "new_failures"
    assert record["new_failures"] == [node]
    assert record["reruns_skipped"] == "script_test_rerun_unavailable"
    result = script_tester.record_to_result("test-command-unavailable", record)
    assert result.status == "failed"
    assert result.findings == [f"new failure: {node}"]
    from agent_crew.loop import handle_test_result
    assert handle_test_result(result) == "failed"
    assert calls == [command, command]


def test_isolated_reruns_share_bounded_watchdog_allowance(project, tmp_path):
    repo, head_sha = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert False\n"})
    base_sha, _ = script_tester.resolve_refs(
        repo, base_branch="main", pr_number=PR, head_sha=head_sha, branch="feature")
    node = "tests/test_b.py::test_two"
    rerun_timeouts = []

    def runner(command, cwd, timeout_s, *, env=None):
        if command != PYTEST:
            rerun_timeouts.append(timeout_s)
            return script_tester.RunOutcome(exit_code=1, failing_ids=[node])
        if "test_b.py" in os.listdir(os.path.join(cwd, "tests")):
            return script_tester.RunOutcome(exit_code=1, failing_ids=[node])
        return script_tester.RunOutcome(exit_code=0)

    record = script_tester.run_script_test(
        repo, base_sha=base_sha, head_sha=head_sha,
        state={"test_command": PYTEST, "test_timeout_s": 1800},
        cache_dir=str(tmp_path / "cache"), runner=runner)
    assert record["outcome"] == "FAILED"
    assert len(rerun_timeouts) == 2
    assert all(0 < timeout <= 600 for timeout in rerun_timeouts)
    assert record["rerun_budget_s"] == 600


@pytest.mark.parametrize("candidate_count", [3, 4, 5])
def test_default_limit_reruns_three_through_five_candidates(project, tmp_path, candidate_count):
    repo, head_sha = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert False\n"})
    base_sha, _ = script_tester.resolve_refs(
        repo, base_branch="main", pr_number=PR, head_sha=head_sha, branch="feature")
    ids = [f"tests/test_b.py::test_case_{i}" for i in range(candidate_count)]
    calls = []

    def runner(command, cwd, timeout_s, *, env=None):
        calls.append(command)
        if command != PYTEST:
            return script_tester.RunOutcome(exit_code=0)
        if "test_b.py" in os.listdir(os.path.join(cwd, "tests")):
            return script_tester.RunOutcome(exit_code=1, failing_ids=ids)
        return script_tester.RunOutcome(exit_code=0)

    record = script_tester.run_script_test(
        repo, base_sha=base_sha, head_sha=head_sha,
        state={"test_command": PYTEST}, cache_dir=str(tmp_path / "cache"), runner=runner)
    assert record["outcome"] == "PASSED"
    assert set(record["flaky"]) == set(ids)
    assert len(calls) == candidate_count + 2
    assert "reruns_skipped" not in record


def test_rerun_limit_and_budget_config_from_env_and_state(monkeypatch, tmp_path, caplog):
    state = {"test_command": PYTEST, "max_rerun_candidates": 4,
             "rerun_budget_s": 240}
    config = script_tester.resolve_test_config(state, str(tmp_path))
    assert config.max_rerun_candidates == 4
    assert config.rerun_budget_s == 240
    monkeypatch.setenv("AGENT_CREW_SCRIPT_TEST_MAX_RERUN_CANDIDATES", "3")
    monkeypatch.setenv("AGENT_CREW_SCRIPT_TEST_RERUN_BUDGET_S", "90.5")
    config = script_tester.resolve_test_config(state, str(tmp_path))
    assert config.max_rerun_candidates == 3
    assert config.rerun_budget_s == 90.5
    monkeypatch.setenv("AGENT_CREW_SCRIPT_TEST_MAX_RERUN_CANDIDATES", "0")
    monkeypatch.setenv("AGENT_CREW_SCRIPT_TEST_RERUN_BUDGET_S", "nan")
    with caplog.at_level("WARNING"):
        config = script_tester.resolve_test_config(state, str(tmp_path))
    assert config.max_rerun_candidates == 5
    assert config.rerun_budget_s == 600
    assert "AGENT_CREW_SCRIPT_TEST_MAX_RERUN_CANDIDATES" in caplog.text
    assert "AGENT_CREW_SCRIPT_TEST_RERUN_BUDGET_S" in caplog.text
    monkeypatch.delenv("AGENT_CREW_SCRIPT_TEST_MAX_RERUN_CANDIDATES")
    monkeypatch.delenv("AGENT_CREW_SCRIPT_TEST_RERUN_BUDGET_S")
    with caplog.at_level("WARNING"):
        config = script_tester.resolve_test_config(
            {"max_rerun_candidates": "many", "rerun_budget_s": -1}, str(tmp_path))
    assert (config.max_rerun_candidates, config.rerun_budget_s) == (5, 600)
    assert "max_rerun_candidates" in caplog.text
    assert "rerun_budget_s" in caplog.text


def test_configured_rerun_budget_limits_attempt_time(project, tmp_path, monkeypatch):
    repo, head_sha = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert False\n"})
    base_sha, _ = script_tester.resolve_refs(
        repo, base_branch="main", pr_number=PR, head_sha=head_sha, branch="feature")
    node = "tests/test_b.py::test_two"
    timeouts = []
    monkeypatch.setenv("AGENT_CREW_SCRIPT_TEST_RERUN_BUDGET_S", "0.05")

    def runner(command, cwd, timeout_s, *, env=None):
        if command != PYTEST:
            timeouts.append(timeout_s)
            return script_tester.RunOutcome(exit_code=0)
        if "test_b.py" in os.listdir(os.path.join(cwd, "tests")):
            return script_tester.RunOutcome(exit_code=1, failing_ids=[node])
        return script_tester.RunOutcome(exit_code=0)

    record = script_tester.run_script_test(
        repo, base_sha=base_sha, head_sha=head_sha,
        state={"test_command": PYTEST}, cache_dir=str(tmp_path / "cache"), runner=runner)
    assert record["rerun_budget_s"] == 0.05
    assert len(timeouts) == 1 and 0 < timeouts[0] <= 0.05


def test_unjudged_rerun_is_incomplete_and_notifies_once(project, server, monkeypatch):
    build, pushes, spawns, merges = server
    repo, head = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert False\n"})
    notices = []
    app, queue = build(repo, {"test_command": PYTEST}, notices=notices)
    node = "tests/test_b.py::test_two"

    def runner(command, cwd, timeout_s, *, env=None):
        if command != PYTEST:
            return script_tester.RunOutcome(exit_code=2, output_tail="collection failed")
        if "test_b.py" in os.listdir(os.path.join(cwd, "tests")):
            return script_tester.RunOutcome(exit_code=1, failing_ids=[node])
        return script_tester.RunOutcome(exit_code=0)

    execute = script_tester.execute_test_task
    monkeypatch.setattr(script_tester, "execute_test_task", lambda *args, **kwargs:
                        execute(*args, **kwargs, runner=runner))
    done = _run_through_push_path(app, queue, _task(head))
    assert done.status == "needs_human"
    assert queue.get_task_context(done.task_id)["script_test"]["reason"] == (
        "script_test_rerun_unjudged")
    assert [task.task_id for task in queue.list_tasks()] == [done.task_id]
    assert len(notices) == 1 and "script_test_rerun_unjudged" in notices[0]
    assert (pushes, spawns, merges) == ([], [], [])


def test_collection_abort_shared_with_base_is_not_a_pass(project, tmp_path):
    broken = {"tests/test_a.py": "def test_one():\n    assert True\n",
              "tests/test_bad.py": "import does_not_exist\n"}
    repo, head = project(broken, {"tests/test_b.py": "def test_two():\n    assert True\n"})
    result = _execute(repo, head, tmp_path)
    record = result.error_info["script_test"]
    assert record["pre_existing"] == []  # collection path is not a pytest node id
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
    assert script_tester.resolve_test_config({}, str(tmp_path)) == (
        None, 1800.0, 5, 600.0)
    (tmp_path / "tests").mkdir()
    assert script_tester.resolve_test_config({}, str(tmp_path))[0] == "python3 -m pytest -q"
    assert script_tester.resolve_test_config(
        {"test_command": "", "test_timeout_s": 60}, str(tmp_path)) == (
            None, 60.0, 5, 600.0)
    assert script_tester.resolve_test_config(
        {"test_command": "make test"}, str(tmp_path))[0] == "make test"


def test_timeout_fails_the_stage(tmp_path):
    outcome = script_tester.run_command("sleep 5", str(tmp_path), 0.2)
    assert outcome.timed_out and outcome.exit_code is None


@pytest.mark.parametrize("timeout_on", ["head", "base"])
def test_timeout_is_incomplete_with_duration_and_no_verdict(project, tmp_path, timeout_on):
    repo, head_sha = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert False\n"})
    base_sha, _ = script_tester.resolve_refs(
        repo, base_branch="main", pr_number=PR, head_sha=head_sha, branch="feature")
    calls = []

    def runner(command, cwd, timeout_s, *, env=None):
        calls.append(cwd)
        if timeout_on == "head" or len(calls) == 2:
            return script_tester.RunOutcome(
                exit_code=None, timed_out=True, duration_s=12,
                failing_ids=["tests/test_b.py::test_two"])
        return script_tester.RunOutcome(
            exit_code=1, timed_out=False, duration_s=2,
            failing_ids=["tests/test_b.py::test_two"])

    record = script_tester.run_script_test(
        repo, base_sha=base_sha, head_sha=head_sha,
        state={"test_command": PYTEST, "test_timeout_s": 12},
        cache_dir=str(tmp_path / "cache"), runner=runner)
    assert len(calls) == (1 if timeout_on == "head" else 2)
    assert record["outcome"] == "INCOMPLETE"
    assert record["reason"] == "script_test_timeout"
    assert record["timeout_s"] == 12
    assert record["duration_s"] == (12 if timeout_on == "head" else 14)
    result = script_tester.record_to_result("test-timeout", record)
    assert result.status == "needs_human"
    assert result.error_info["reason"] == "script_test_timeout"
    from agent_crew.loop import handle_test_result
    assert handle_test_result(result) == "not_run"


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
    queue.patch_context(f"test-{claim_source}", {
        "script_test_timeout_s": 1200, "script_test_rerun_budget_s": 900})
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
        rerun_still_running = app.state.watchdog_tick(started_at + 3001)
        assert rerun_still_running["timed_out"] == []
        late = app.state.watchdog_tick(started_at + 3901)
        assert late["timed_out"] == [f"test-{claim_source}"]


def test_watchdog_includes_capped_configured_rerun_budget(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_SCRIPT_TEST_RERUN_BUDGET_S", "1500")
    assert script_tester.watchdog_timeout_seconds(
        {"test_timeout_s": 1200}, 900) == 4200
    monkeypatch.setenv("AGENT_CREW_SCRIPT_TEST_RERUN_BUDGET_S", "300")
    assert script_tester.watchdog_timeout_seconds(
        {"test_timeout_s": 1200}, 900) == 3300


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

    def build(repo, state, *, notices=None):
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps({"project": "proj", **state}))
        db = str(tmp_path / "tasks.db")
        app = create_app(db, pane_map={"tester": "%9"}, project="proj",
                         push_fn=lambda pane, text: pushes.append(pane),
                         coordinator_notify_fn=(
                             (lambda message: notices.append(message) or True)
                             if notices is not None else None),
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


def _assert_script_attribution(queue, task_id, *, outcome, suite_started):
    with sqlite3.connect(queue._db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM task_attribution WHERE task_id=?", (task_id,)
        ).fetchone()[0]
    assert count == 1
    attr = queue.get_attribution(task_id)
    assert (attr["agent"], attr["role"], attr["task_type"]) == (
        "script", "tester", "test")
    assert attr["outcome"] == outcome
    assert attr["provider_session_id"] is None
    assert attr["started_at"] > 0
    assert attr["completed_at"] >= attr["started_at"]
    for field in ("uncached_input_tokens", "cache_write_tokens", "cache_read_tokens",
                  "output_tokens", "reasoning_tokens"):
        assert attr[field] == (0 if suite_started else None), field


def test_server_runs_test_stage_by_script_and_merges_on_pass(project, server):
    build, pushes, spawns, merges = server
    repo, head = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert True\n"})
    app, queue = build(repo, {"test_command": PYTEST})
    done = _run_through_push_path(app, queue, _task(head))
    assert done.status == "completed"
    assert queue.get_task_context(done.task_id)["script_test"]["counts"] == {"passed": 2}
    assert queue.get_task_context(done.task_id)["script_test_timeout_s"] == 1800
    assert queue.get_task_context(done.task_id)["script_test_rerun_budget_s"] == 600
    assert (pushes, spawns) == ([], [])          # 0 provider calls
    assert merges == [f"merge:pr:{PR}"]
    _assert_script_attribution(queue, done.task_id, outcome="completed", suite_started=True)


def test_server_persists_configured_rerun_budget(project, server):
    build, _, _, _ = server
    repo, head = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert True\n"})
    app, queue = build(repo, {"test_command": PYTEST, "rerun_budget_s": 900})

    done = _run_through_push_path(app, queue, _task(head))

    assert done.status == "completed"
    assert queue.get_task_context(done.task_id)["script_test_rerun_budget_s"] == 900


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
    _assert_script_attribution(queue, done.task_id, outcome="failed", suite_started=True)


def test_server_flaky_head_failure_has_no_implement_successor(
        project, server, monkeypatch):
    build, pushes, spawns, _ = server
    repo, head = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert False\n"})
    app, queue = build(repo, {"test_command": PYTEST})
    node = "tests/test_b.py::test_two"

    def runner(command, cwd, timeout_s, *, env=None):
        if command != PYTEST:
            return script_tester.RunOutcome(exit_code=0, counts={"passed": 1})
        if "test_b.py" in os.listdir(os.path.join(cwd, "tests")):
            return script_tester.RunOutcome(exit_code=1, failing_ids=[node])
        return script_tester.RunOutcome(exit_code=0)

    execute = script_tester.execute_test_task
    monkeypatch.setattr(script_tester, "execute_test_task", lambda *args, **kwargs:
                        execute(*args, **kwargs, runner=runner))
    done = _run_through_push_path(app, queue, _task(head))
    assert done.status == "completed"
    assert queue.get_task_context(done.task_id)["script_test"]["flaky"] == [node]
    assert [task.task_id for task in queue.list_tasks()] == [done.task_id]
    assert (pushes, spawns) == ([], [])


def test_server_no_command_never_auto_merges(project, server):
    build, pushes, spawns, merges = server
    repo, head = project({"README": "x\n"}, {"README": "y\n"})
    app, queue = build(repo, {})
    done = _run_through_push_path(app, queue, _task(head))
    assert done.status == "needs_human"
    assert queue.get_task_context(done.task_id)["script_test"]["outcome"] == (
        "SKIPPED_NO_TEST_COMMAND")
    assert (pushes, spawns, merges) == ([], [], [])
    _assert_script_attribution(queue, done.task_id, outcome="not_run", suite_started=False)


def test_server_collection_error_has_no_test_verdict_in_attribution(project, server):
    build, pushes, spawns, merges = server
    repo, head = project(GREEN, {"tests/test_bad.py": "import does_not_exist\n"})
    app, queue = build(repo, {"test_command": PYTEST})
    done = _run_through_push_path(app, queue, _task(head))
    assert done.status == "failed"
    assert queue.get_task_context(done.task_id)["script_test"]["reason"] == "exit_code_2"
    _assert_script_attribution(queue, done.task_id, outcome="not_run", suite_started=True)
    assert (pushes, spawns, merges) == ([], [], [])


@pytest.mark.parametrize("suite_started", [False, True])
def test_server_timeout_never_enqueues_implement_and_notifies_once(
        project, server, monkeypatch, suite_started):
    build, pushes, spawns, merges = server
    repo, head = project(GREEN, {"tests/test_b.py": "def test_two():\n    assert True\n"})
    notices = []
    app, queue = build(repo, {"test_command": PYTEST, "test_timeout_s": 12},
                       notices=notices)
    record = {"outcome": "INCOMPLETE", "reason": "script_test_timeout",
              "timeout_s": 12, "duration_s": 12}
    if suite_started:
        record["head"] = script_tester.RunOutcome(
            exit_code=None, timed_out=True, duration_s=12).to_dict()
    monkeypatch.setattr(script_tester, "execute_test_task", lambda *a, **k:
                        script_tester.record_to_result("test-review-1", record, PR))
    done = _run_through_push_path(app, queue, _task(head))
    assert done.status == "needs_human"
    assert [t.task_id for t in queue.list_tasks()] == [done.task_id]
    assert len(notices) == 1 and "script_test_timeout" in notices[0]
    assert (pushes, spawns, merges) == ([], [], [])
    _assert_script_attribution(queue, done.task_id, outcome="not_run",
                               suite_started=suite_started)


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
    def __init__(self, results, *, duplicate_review=False):
        self.results = list(results)
        self.index = 0
        self.rows = []
        self.duplicate_review = duplicate_review

    def list_tasks(self):
        return list(self.rows)

    def get_result(self, task_id):
        result = self.results[self.index]
        self.index += 1
        if self.index < len(self.results):
            successor = self.results[self.index]
            task_type = ("review" if result.task_id.startswith("impl-")
                         and successor.task_id.startswith("review-") else
                         "test" if result.task_id.startswith("review-")
                         and successor.task_id.startswith("test-") else "")
            if task_type:
                self.rows.append(TaskRequest(
                    task_id=successor.task_id, task_type=task_type,
                    description="server successor", branch="main",
                    context={"prev_task_id": task_id}))
                if task_type == "review" and self.duplicate_review:
                    self.rows.append(TaskRequest(
                        task_id="review-duplicate", task_type="review",
                        description="duplicate", branch="main",
                        context={"prev_task_id": task_id}))
        return result


def _crew_run(monkeypatch, tmp_path, results, *, duplicate_review=False, tester=False):
    import agent_crew.loop as loop
    from agent_crew.cli import crew

    dispatched = []

    def enqueue_implement(queue, task, branch, context=None, port=0):
        dispatched.append("implement")
        return f"impl-{dispatched.count('implement')}"

    queue = _LoopQueue(results, duplicate_review=duplicate_review)
    monkeypatch.setattr("agent_crew.queue.TaskQueue", lambda db: queue)
    monkeypatch.setattr(loop, "enqueue_implement", enqueue_implement)
    monkeypatch.setattr(loop, "enqueue_review", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("CLI enqueued review")))
    monkeypatch.setattr(loop, "enqueue_test", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("CLI enqueued test")))
    args = ["run", "work", "--db", str(tmp_path / "tasks.db"), "--max-iter", "2",
            "--timeout", "1"]
    if not tester:
        args.append("--no-tester")
    return CliRunner().invoke(crew, args), dispatched


def _impl_done():
    from agent_crew.protocol import TaskResult
    return TaskResult("impl-1", "completed", "done", branch="main", commit=COMMIT)


def test_run_loop_missing_server_review_stops_without_enqueuing(monkeypatch, tmp_path):
    out, dispatched = _crew_run(monkeypatch, tmp_path, [_impl_done()])
    assert out.exit_code != 0
    assert "Server did not create a review successor" in out.output
    assert dispatched == ["implement"]


def test_run_loop_ambiguous_server_reviews_stops_without_enqueuing(monkeypatch, tmp_path):
    out, dispatched = _crew_run(
        monkeypatch, tmp_path, [_impl_done(),
                                TaskResult("review-1", "completed", "ok", verdict="approve")],
        duplicate_review=True)
    assert out.exit_code != 0
    assert "Multiple review successors" in out.output
    assert dispatched == ["implement"]


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
    assert dispatched == ["implement"]


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
    assert dispatched == ["implement"]
