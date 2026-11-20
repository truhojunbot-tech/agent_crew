"""#1038: a local script-test pass is not vetoed by CI that never ran a job."""

import logging

import pytest

from agent_crew import script_tester
from agent_crew.protocol import TaskRequest


def _run(monkeypatch, ci_state, *, local=script_tester.PASSED, workflows=True):
    record = {"outcome": local, "counts": {"passed": 79}}
    if local == script_tester.FAILED:
        record.update(counts={"passed": 78, "failed": 1}, reason="new_failures",
                      new_failures=["tests/test_a.py::test_x"])
    monkeypatch.setattr(script_tester, "resolve_refs", lambda *a, **k: ("base", "a" * 40))
    monkeypatch.setattr(script_tester, "run_script_test", lambda *a, **k: dict(record))
    monkeypatch.setattr(script_tester, "head_has_workflows", lambda *a: workflows)
    monkeypatch.setattr("agent_crew.github.head_checks_state",
                        lambda repo, sha: (ci_state, f"{ci_state} detail"))
    task = TaskRequest(task_id="test-1038", task_type="test", description="test",
                       branch="feature", context={"repo": "owner/repo", "base_branch": "main"})
    return script_tester.execute_test_task(task, repo="/checkout", state=None,
                                           cache_dir="/cache")


@pytest.fixture(autouse=True)
def _gate_unset(monkeypatch):
    monkeypatch.delenv(script_tester.CI_GATE_ENV, raising=False)


@pytest.mark.parametrize("ci_state", ["none", "infra_unavailable"])
def test_local_pass_with_ci_that_ran_no_job_stays_passed(monkeypatch, ci_state):
    result = _run(monkeypatch, ci_state)
    record = result.error_info["script_test"]
    assert result.status == "completed"
    assert record["outcome"] == script_tester.PASSED
    assert record["ci_state"] == ci_state
    assert record["ci_detail"] == f"{ci_state} detail"
    assert "reason" not in record
    assert f"ci_state={ci_state} (not gated: infra)" in result.summary


def test_real_failed_job_with_gate_on_fails_ci_red(monkeypatch):
    result = _run(monkeypatch, "red")
    assert result.status == "failed"
    assert result.error_info["script_test"]["reason"] == "ci_red"
    assert result.error_info["reason"] == "script_test_ci_red"


@pytest.mark.parametrize("value", ["1", " 1 "])
def test_gate_explicitly_on_still_fails_red(monkeypatch, value):
    monkeypatch.setenv(script_tester.CI_GATE_ENV, value)
    assert _run(monkeypatch, "red").status == "failed"


def test_gate_off_keeps_local_pass_over_red(monkeypatch):
    monkeypatch.setenv(script_tester.CI_GATE_ENV, "0")
    result = _run(monkeypatch, "red")
    record = result.error_info["script_test"]
    assert result.status == "completed"
    assert record["outcome"] == script_tester.PASSED
    assert (record["ci_state"], record["ci_detail"]) == ("red", "red detail")
    assert "ci_state=red (not gated: gate off)" in result.summary


@pytest.mark.parametrize("gate", [None, "0"])
@pytest.mark.parametrize("ci_state", ["green", "red", "none"])
def test_local_failure_fails_regardless_of_ci(monkeypatch, gate, ci_state):
    if gate is not None:
        monkeypatch.setenv(script_tester.CI_GATE_ENV, gate)
    result = _run(monkeypatch, ci_state, local=script_tester.FAILED)
    assert result.status == "failed"
    assert result.error_info["script_test"]["reason"] == "new_failures"
    assert "ci_state" not in result.error_info["script_test"]


def test_gate_unset_and_green_is_unchanged(monkeypatch):
    result = _run(monkeypatch, "green")
    record = result.error_info["script_test"]
    assert result.status == "completed"
    assert record["ci_state"] == "green"
    assert "ci_not_gated" not in record
    assert "not gated" not in result.summary


@pytest.mark.parametrize("ci_state", ["pending", "error", "mystery"])
def test_ci_not_proving_no_failing_job_stays_incomplete(monkeypatch, ci_state):
    """A running job may still fail; an unreadable lookup is no evidence at all."""
    result = _run(monkeypatch, ci_state)
    record = result.error_info["script_test"]
    assert result.status == "needs_human"
    assert record["outcome"] == script_tester.INCOMPLETE
    assert record["reason"] == f"ci_{ci_state}"
    assert "ci_not_gated" not in record


@pytest.mark.parametrize("ci_state", ["pending", "error"])
def test_gate_off_keeps_local_pass_over_unproven_ci(monkeypatch, ci_state):
    monkeypatch.setenv(script_tester.CI_GATE_ENV, "0")
    result = _run(monkeypatch, ci_state)
    assert result.status == "completed"
    assert result.error_info["script_test"]["ci_not_gated"] == "gate off"


def test_unrecognised_ci_state_stays_incomplete(monkeypatch):
    result = _run(monkeypatch, "mystery")
    assert result.status == "needs_human"
    assert result.error_info["script_test"]["reason"] == "ci_mystery"


@pytest.mark.parametrize("value", ["yes", "", "2", "off"])
def test_bad_gate_value_warns_and_uses_default(monkeypatch, caplog, value):
    monkeypatch.setenv(script_tester.CI_GATE_ENV, value)
    with caplog.at_level(logging.WARNING, logger=script_tester.logger.name):
        assert script_tester.ci_gate_enabled() is True
        assert _run(monkeypatch, "red").status == "failed"
    assert any(script_tester.CI_GATE_ENV in r.getMessage() for r in caplog.records)


def _gh_checks(monkeypatch, check_runs, *, lookup_rc=0, job=None):
    """Drive the real ``head_checks_state`` classifier with canned ``gh api`` replies."""
    import json
    import subprocess

    from agent_crew import github

    def run(args, **kwargs):
        path = args[2] if args[:2] == ["gh", "api"] else ""
        if "/check-runs" in path:
            return subprocess.CompletedProcess(
                args, lookup_rc, json.dumps({"check_runs": check_runs}), "")
        if path.endswith("/status"):
            return subprocess.CompletedProcess(args, 0, json.dumps({"statuses": []}), "")
        if "/actions/jobs/" in path:
            return subprocess.CompletedProcess(args, 0, json.dumps(job or {}), "")
        raise AssertionError(f"unexpected subprocess call {args}")

    monkeypatch.setattr(github, "check_gh_installed", lambda: True)
    monkeypatch.setattr(github.subprocess, "run", run)
    monkeypatch.setattr(script_tester, "resolve_refs", lambda *a, **k: ("base", "a" * 40))
    monkeypatch.setattr(script_tester, "run_script_test",
                        lambda *a, **k: {"outcome": script_tester.PASSED,
                                         "counts": {"passed": 79}})
    monkeypatch.setattr(script_tester, "head_has_workflows", lambda *a: True)
    task = TaskRequest(task_id="test-1038", task_type="test", description="test",
                       branch="feature", context={"repo": "owner/repo", "base_branch": "main"})
    return script_tester.execute_test_task(task, repo="/checkout", state=None,
                                           cache_dir="/cache")


_ACTIONS = {"slug": "github-actions"}


def test_started_in_progress_job_keeps_local_pass_incomplete(monkeypatch):
    """A runner executing steps may still fail after the stage unlocks a merge."""
    result = _gh_checks(monkeypatch, [{"id": 7, "name": "tests", "app": _ACTIONS,
                                      "status": "in_progress", "conclusion": None}])
    record = result.error_info["script_test"]
    assert result.status == "needs_human"
    assert (record["ci_state"], record["reason"]) == ("pending", "ci_pending")


def test_failed_gh_lookup_keeps_local_pass_incomplete(monkeypatch):
    result = _gh_checks(monkeypatch, [], lookup_rc=1)
    record = result.error_info["script_test"]
    assert result.status == "needs_human"
    assert (record["ci_state"], record["reason"]) == ("error", "ci_error")


def test_billing_blocked_never_started_job_keeps_local_pass(monkeypatch):
    result = _gh_checks(
        monkeypatch,
        [{"id": 7, "name": "tests", "app": _ACTIONS,
          "status": "completed", "conclusion": "failure"}],
        job={"runner_name": None, "runner_id": None, "steps": [],
             "html_url": "https://github.com/owner/repo/actions/runs/1/job/7"})
    record = result.error_info["script_test"]
    assert result.status == "completed"
    assert (record["ci_state"], record["ci_not_gated"]) == ("infra_unavailable", "infra")


def test_job_that_ran_a_step_and_failed_is_red(monkeypatch):
    result = _gh_checks(
        monkeypatch,
        [{"id": 7, "name": "tests", "app": _ACTIONS,
          "status": "completed", "conclusion": "failure"}],
        job={"runner_name": "gh-1", "runner_id": 3,
             "steps": [{"name": "pytest", "conclusion": "failure"}]})
    assert result.status == "failed"
    assert result.error_info["script_test"]["reason"] == "ci_red"
