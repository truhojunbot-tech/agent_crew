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


@pytest.mark.parametrize("ci_state", ["none", "pending", "infra_unavailable", "error"])
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
