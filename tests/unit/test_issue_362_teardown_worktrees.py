"""#362 teardown must unregister clean worktrees and preserve unsafe ones."""

import json
import shutil
import subprocess

import pytest
from click.testing import CliRunner

from agent_crew.cli import crew


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                          capture_output=True)


@pytest.fixture
def worktree_project(tmp_path):
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    worktree = tmp_path / "crew-worktree"
    _git("init", "--bare", str(origin), cwd=tmp_path)
    _git("init", "-b", "main", str(repo), cwd=tmp_path)
    _git("config", "user.email", "crew@example.test", cwd=repo)
    _git("config", "user.name", "Crew Test", cwd=repo)
    (repo / "README").write_text("base\n")
    _git("add", "README", cwd=repo)
    _git("commit", "-m", "base", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    _git("push", "-u", "origin", "main", cwd=repo)
    _git("worktree", "add", "-b", "agent/crew-test", str(worktree), "main", cwd=repo)
    return repo, worktree


def _state(base, repo, worktree):
    project = "teardown-project"
    project_dir = base / project
    project_dir.mkdir()
    (project_dir / "state.json").write_text(json.dumps({
        "project": project, "session": "unused", "agents": ["claude"],
        "worktrees": {"claude": str(worktree)}, "repo_path": str(repo), "server_pid": 0,
    }))
    return project


def _listed(repo, worktree):
    return str(worktree) in _git("worktree", "list", "--porcelain", cwd=repo).stdout


def test_teardown_removes_clean_worktree_and_registration(tmp_path, worktree_project, monkeypatch):
    repo, worktree = worktree_project
    project = _state(tmp_path, repo, worktree)
    monkeypatch.setattr("agent_crew.cli._tmux_snapshot", lambda _session: "")

    result = CliRunner().invoke(crew, ["teardown", project, "--base", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert not worktree.exists()
    assert not _listed(repo, worktree)


def test_teardown_preserves_dirty_worktree_and_reports_command(tmp_path, worktree_project, monkeypatch):
    repo, worktree = worktree_project
    project = _state(tmp_path, repo, worktree)
    (worktree / "dirty.txt").write_text("do not lose\n")
    monkeypatch.setattr("agent_crew.cli._tmux_snapshot", lambda _session: "")

    result = CliRunner().invoke(crew, ["teardown", project, "--base", str(tmp_path)])

    assert result.exit_code != 0
    assert worktree.exists() and _listed(repo, worktree)
    assert f"git -C {worktree} status --porcelain" in result.output


def test_teardown_preserves_unpushed_commit_and_reports_command(tmp_path, worktree_project, monkeypatch):
    repo, worktree = worktree_project
    project = _state(tmp_path, repo, worktree)
    (worktree / "local.txt").write_text("seven commits would be protected too\n")
    _git("add", "local.txt", cwd=worktree)
    _git("commit", "-m", "local only", cwd=worktree)
    monkeypatch.setattr("agent_crew.cli._tmux_snapshot", lambda _session: "")

    result = CliRunner().invoke(crew, ["teardown", project, "--base", str(tmp_path)])

    assert result.exit_code != 0
    assert worktree.exists() and _listed(repo, worktree)
    assert f"git -C {worktree} log --oneline HEAD --not --remotes" in result.output


def test_teardown_prunes_registration_for_already_missing_worktree(tmp_path, worktree_project, monkeypatch):
    repo, worktree = worktree_project
    project = _state(tmp_path, repo, worktree)
    shutil.rmtree(worktree)
    assert _listed(repo, worktree)
    monkeypatch.setattr("agent_crew.cli._tmux_snapshot", lambda _session: "")

    result = CliRunner().invoke(crew, ["teardown", project, "--base", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert not _listed(repo, worktree)
