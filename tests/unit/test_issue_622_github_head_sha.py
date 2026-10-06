"""PR head lookup accepts the repository forms used by review contexts (#622)."""

import subprocess

import pytest

from agent_crew import github


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
