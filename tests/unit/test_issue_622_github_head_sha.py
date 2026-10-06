"""GitHub API lookups accept the repository forms used by review contexts (#622)."""

import json
import subprocess

import pytest

from agent_crew import github
from agent_crew.github import publish_independent_review_status as real_publish_status


@pytest.mark.parametrize("repo", [
    "git@github.com:o/r",
    "git@github.com:o/r.git",
    "https://github.com/o/r.git",
    "o/r",
])
def test_pr_head_sha_normalizes_repo(repo, monkeypatch):
    calls = []
    monkeypatch.setattr(github, "check_gh_installed", lambda: True)

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")

    monkeypatch.setattr(github.subprocess, "run", run)
    assert github.pr_head_sha(42, repo=repo) == "a" * 40
    assert calls == [["gh", "api", "repos/o/r/pulls/42", "--jq", ".head.sha"]]


@pytest.mark.parametrize("repo", ["", "not-a-repo", "o/r/extra", "https://gitlab.com/o/r"])
def test_pr_head_sha_rejects_invalid_repo(repo, monkeypatch):
    monkeypatch.setattr(github, "check_gh_installed", lambda: True)
    monkeypatch.setattr(github, "get_repo", lambda **kwargs: "")
    monkeypatch.setattr(github.subprocess, "run",
                        lambda *a, **k: pytest.fail("invalid repo reached gh api"))
    assert github.pr_head_sha(42, repo=repo) == ""


def test_independent_review_status_reads_ssh_repo(monkeypatch):
    sha = "a" * 40
    calls = []
    monkeypatch.setattr(github, "check_gh_installed", lambda: True)
    monkeypatch.setattr(github, "pr_head_sha", lambda *a, **k: sha)

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps({"statuses": [
            {"context": "crew/independent-review", "state": "success"}]}), "")

    monkeypatch.setattr(github.subprocess, "run", run)
    assert github.independent_review_succeeded(42, "git@github.com:o/r")
    assert calls == [["gh", "api", f"repos/o/r/commits/{sha}/status"]]


def test_publish_independent_review_status_uses_ssh_repo(monkeypatch):
    sha = "a" * 40
    calls = []
    monkeypatch.setattr(github, "check_gh_installed", lambda: True)

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(github.subprocess, "run", run)
    assert real_publish_status(
        "git@github.com:o/r", sha, "review-42", "codex")
    assert calls[0][:5] == ["gh", "api", "-X", "POST", f"repos/o/r/statuses/{sha}"]


def test_branch_commit_message_uses_ssh_repo(monkeypatch):
    calls = []
    monkeypatch.setattr(github, "check_gh_installed", lambda: True)

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "commit message\n", "")

    monkeypatch.setattr(github.subprocess, "run", run)
    assert github.branch_head_commit_message("feature", repo="git@github.com:o/r") == "commit message"
    assert calls == [["gh", "api", "repos/o/r/commits/feature", "--jq", ".commit.message"]]
