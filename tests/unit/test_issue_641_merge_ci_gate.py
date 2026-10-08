"""GitHub CI evidence used by the server merge gate (#641)."""

import json
import subprocess

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
    monkeypatch.setattr(github.subprocess, "run",
                        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", "oops"))
    assert github.head_checks_state("o/r", sha)[0] == "error"
