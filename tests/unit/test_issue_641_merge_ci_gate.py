"""GitHub CI evidence used by the server merge gate (#641)."""

import json
import subprocess

import pytest

from agent_crew import github


def test_head_checks_reads_runs_and_statuses(monkeypatch):
    sha = "a" * 40
    calls = []
    monkeypatch.setattr(github, "check_gh_installed", lambda: True)

    def run(args, **kwargs):
        calls.append(args)
        if "check-runs" in args[2]:
            body = {"check_runs": [
                {"name": "skip", "status": "completed", "conclusion": "skipped"},
                {"name": "unit", "status": "completed", "conclusion": "failure"}]}
            return subprocess.CompletedProcess(args, 0, json.dumps(body) + json.dumps({
                "check_runs": []}), "")
        else:
            body = {"statuses": [{"context": "crew/independent-review",
                                  "state": "success"}]}
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    monkeypatch.setattr(github.subprocess, "run", run)
    assert github.head_checks_state("git@github.com:o/r.git", sha) == ("red", "unit")
    assert calls[0][:3] == ["gh", "api", f"repos/o/r/commits/{sha}/check-runs?per_page=100"]
    assert calls[1][:3] == ["gh", "api", f"repos/o/r/commits/{sha}/status"]


def test_head_checks_pending_none_and_error(monkeypatch):
    sha = "b" * 40
    monkeypatch.setattr(github, "check_gh_installed", lambda: True)
    data = {"check_runs": [{"name": "unit", "status": "in_progress",
                            "conclusion": None}]}

    def run(args, **kwargs):
        if "check-runs" in args[2]:
            return subprocess.CompletedProcess(args, 0, json.dumps(data), "")
        return subprocess.CompletedProcess(args, 0, '{"statuses": []}', "")

    monkeypatch.setattr(github.subprocess, "run", run)
    assert github.head_checks_state("o/r", sha) == ("pending", "unit")
    data["check_runs"] = []
    assert github.head_checks_state("o/r", sha) == ("none", "")
    data["check_runs"] = [{"name": "skip", "status": "completed", "conclusion": "skipped"}]
    assert github.head_checks_state("o/r", sha) == ("none", "")
    monkeypatch.setattr(github.subprocess, "run",
                        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", "oops"))
    assert github.head_checks_state("o/r", sha)[0] == "error"


@pytest.mark.parametrize("case,job,other_run,expected", [
    ("never_started", {"runner_name": "", "runner_id": None, "steps": []},
     None, "infra_unavailable"),
    ("real_failure", {"runner_name": "runner-1", "runner_id": 9,
                      "steps": [{"name": "test", "conclusion": "failure"}]},
     None, "red"),
    ("mixed", {"runner_name": "", "runner_id": None, "steps": []},
     {"id": 72, "name": "unit", "status": "completed", "conclusion": "failure",
      "app": {"slug": "github-actions"}}, "red"),
    ("lookup_error", None, None, "red"),
    ("non_actions", None, None, "red"),
])
def test_failed_action_that_never_started_has_its_own_state(
        monkeypatch, case, job, other_run, expected):
    sha = "c" * 40
    run = {"id": 71, "name": "enforce", "status": "completed",
           "conclusion": "failure", "html_url": "https://github.com/o/r/actions/runs/71",
           "app": {"slug": "circleci" if case == "non_actions" else "github-actions"}}
    calls = []
    monkeypatch.setattr(github, "check_gh_installed", lambda: True)

    def api(args, **kwargs):
        calls.append(args[2])
        path = args[2]
        if "/check-runs" in path:
            return subprocess.CompletedProcess(args, 0, json.dumps({
                "check_runs": [run] + ([other_run] if other_run else [])}), "")
        if path.endswith("/status"):
            return subprocess.CompletedProcess(args, 0, '{"statuses": []}', "")
        if case == "lookup_error":
            return subprocess.CompletedProcess(args, 1, "", "unavailable")
        if path.endswith("/actions/jobs/72"):
            return subprocess.CompletedProcess(args, 0, json.dumps({
                "runner_name": "runner-2", "runner_id": 12,
                "steps": [{"name": "test", "conclusion": "failure"}]}), "")
        return subprocess.CompletedProcess(args, 0, json.dumps(job), "")

    monkeypatch.setattr(github.subprocess, "run", api)
    state, detail = github.head_checks_state("o/r", sha)
    assert state == expected
    if expected == "infra_unavailable":
        assert "enforce" in detail and "https://github.com/o/r/actions/runs/71" in detail
    if case == "non_actions":
        assert not any("/actions/jobs/" in path for path in calls)
    else:
        assert "repos/o/r/actions/jobs/71" in calls
