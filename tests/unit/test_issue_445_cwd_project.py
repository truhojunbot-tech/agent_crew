"""A CLI command must never select another crew from state-file activity."""

import json
import os
import subprocess
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from agent_crew.cli import _auto_detect_project, crew


def _repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _state(base, name, **fields):
    directory = base / name
    directory.mkdir()
    (directory / "state.json").write_text(json.dumps({"project": name, "db": str(directory / "tasks.db"), **fields}))
    return directory / "state.json"


def test_cwd_wins_over_newer_state_and_nested_directory(tmp_path, monkeypatch):
    base = tmp_path / "crew"
    base.mkdir()
    a, b = _repo(tmp_path / "repo-a"), _repo(tmp_path / "repo-b")
    state_a = _state(base, "A", repo_path=str(a))
    state_b = _state(base, "B", repo_path=str(b))
    os.utime(state_a, (1, 1))
    os.utime(state_b, None)
    nested = a / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    assert _auto_detect_project(str(base)) == "A"


@pytest.mark.parametrize("command", [
    ["run", "task"], ["task", "cancel", "id"],
    ["task", "expire-stale", "--dry-run"], ["discuss", "topic"],
])
def test_commands_choose_cwd_project_even_when_other_state_is_newer(tmp_path, monkeypatch, command):
    base = tmp_path / "crew"
    base.mkdir()
    a, b = _repo(tmp_path / "repo-a"), _repo(tmp_path / "repo-b")
    state_a = _state(base, "A", repo_path=str(a))
    _state(base, "B", repo_path=str(b))
    os.utime(state_a, (1, 1))
    monkeypatch.chdir(a)
    with patch("agent_crew.cli._read_state", side_effect=RuntimeError("selected project")) as read:
        result = CliRunner().invoke(crew, [*command, "--base", str(base)])
    assert isinstance(result.exception, RuntimeError)
    assert read.call_args.args == (str(base), "A")


def test_legacy_worktree_matches_same_git_common_dir(tmp_path, monkeypatch):
    base = tmp_path / "crew"
    base.mkdir()
    repo = _repo(tmp_path / "repo")
    subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty", "-qm", "init"],
                   check=True, env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
                                    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com"})
    wt = tmp_path / "worker"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(wt)], check=True)
    _state(base, "A", worktrees={"codex": str(wt)})
    monkeypatch.chdir(repo)
    assert _auto_detect_project(str(base)) == "A"


def test_outside_and_ambiguous_cwd_refuse(tmp_path, monkeypatch):
    base = tmp_path / "crew"
    base.mkdir()
    repo = _repo(tmp_path / "repo")
    _state(base, "A", repo_path=str(repo))
    monkeypatch.chdir(tmp_path)
    assert _auto_detect_project(str(base)) is None
    result = CliRunner().invoke(crew, ["run", "task", "--base", str(base)])
    assert result.exit_code != 0 and "--project" in result.output
    _state(base, "B", repo_path=str(repo))
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(crew, ["run", "task", "--base", str(base)])
    assert result.exit_code != 0 and "multiple" in result.output.lower()


@pytest.mark.parametrize("command", [
    ["run", "task"], ["task", "cancel", "id"],
    ["task", "expire-stale", "--dry-run"], ["discuss", "topic"],
])
def test_all_project_commands_refuse_unmatched_cwd(tmp_path, monkeypatch, command):
    base = tmp_path / "crew"
    base.mkdir()
    _state(base, "A", repo_path=str(_repo(tmp_path / "repo")))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(crew, [*command, "--base", str(base)])
    assert result.exit_code != 0 and "--project" in result.output


def test_explicit_project_outside_repo_and_standalone_db_still_work(tmp_path, monkeypatch):
    from agent_crew.queue import TaskQueue

    base = tmp_path / "crew"
    base.mkdir()
    state = _state(base, "A", repo_path=str(_repo(tmp_path / "repo")))
    db = str(state.parent / "tasks.db")
    TaskQueue(db)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    explicit = runner.invoke(crew, ["task", "expire-stale", "--dry-run", "--project", "A", "--base", str(base)])
    standalone = runner.invoke(crew, ["task", "expire-stale", "--dry-run", "--db", db, "--base", str(base)])
    assert explicit.exit_code == 0 and "No stale tasks" in explicit.output
    assert standalone.exit_code == 0 and "No stale tasks" in standalone.output


@pytest.mark.parametrize("command", [
    ["run", "task"], ["task", "cancel", "id"],
    ["task", "expire-stale", "--dry-run"], ["discuss", "topic"],
])
def test_cross_project_needs_explicit_flag(tmp_path, monkeypatch, command):
    base = tmp_path / "crew"
    base.mkdir()
    a, b = _repo(tmp_path / "repo-a"), _repo(tmp_path / "repo-b")
    _state(base, "A", repo_path=str(a))
    _state(base, "B", repo_path=str(b))
    monkeypatch.chdir(a)
    args = [*command, "--project", "B", "--base", str(base)]
    denied = CliRunner().invoke(crew, args)
    assert denied.exit_code != 0 and "--allow-cross-project" in denied.output
    with patch("agent_crew.loop.enqueue_implement", side_effect=RuntimeError("past routing")):
        allowed = CliRunner().invoke(crew, [*args, "--allow-cross-project"])
    assert "Cross-project" in allowed.output
    assert "--allow-cross-project" not in allowed.output


@pytest.mark.parametrize("command", [
    ["run", "task"], ["task", "cancel", "id"],
    ["task", "expire-stale", "--dry-run"], ["discuss", "topic"],
])
def test_instance_marker_routes_all_commands(tmp_path, monkeypatch, command):
    base = tmp_path / "crew"
    base.mkdir()
    repo = _repo(tmp_path / "repo")
    _state(base, "alfred", repo_path=str(repo))
    _state(base, "quota-ops", repo_path=str(_repo(tmp_path / "quota")))
    instance = repo / "instances" / "Quota" / "nested"
    instance.mkdir(parents=True)
    (instance.parent / ".crew-project").write_text("quota-ops\n")
    monkeypatch.chdir(instance)
    assert _auto_detect_project(str(base)) == "quota-ops"
    with patch("agent_crew.cli._read_state", side_effect=RuntimeError("selected project")) as read:
        result = CliRunner().invoke(crew, [*command, "--base", str(base)])
    assert isinstance(result.exception, RuntimeError)
    assert read.call_args.args == (str(base), "quota-ops")


@pytest.mark.parametrize("command", [
    ["run", "task"], ["task", "cancel", "id"],
    ["task", "expire-stale", "--dry-run"], ["discuss", "topic"],
])
def test_unmarked_instance_refuses_all_commands(tmp_path, monkeypatch, command):
    base = tmp_path / "crew"
    base.mkdir()
    repo = _repo(tmp_path / "repo")
    _state(base, "alfred", repo_path=str(repo))
    instance = repo / "instances" / "bot"
    instance.mkdir(parents=True)
    monkeypatch.chdir(instance)
    assert _auto_detect_project(str(base)) is None
    result = CliRunner().invoke(crew, [*command, "--base", str(base)])
    assert result.exit_code != 0
    assert "--project" in result.output and ".crew-project" in result.output


def test_nearest_marker_wins_and_unregistered_marker_refuses(tmp_path, monkeypatch):
    base = tmp_path / "crew"
    base.mkdir()
    repo = _repo(tmp_path / "repo")
    _state(base, "alfred", repo_path=str(repo))
    _state(base, "quota-ops")
    instance = repo / "instances" / "Quota"
    instance.mkdir(parents=True)
    (repo / ".crew-project").write_text("alfred\n")
    (instance / ".crew-project").write_text("quota-ops\n")
    monkeypatch.chdir(instance)
    assert _auto_detect_project(str(base)) == "quota-ops"
    (instance / ".crew-project").write_text("missing\n")
    result = CliRunner().invoke(crew, ["run", "task", "--base", str(base)])
    assert result.exit_code != 0 and "not registered" in result.output


@pytest.mark.parametrize("command", [
    ["run", "task"], ["task", "cancel", "id"],
    ["task", "expire-stale", "--dry-run"], ["discuss", "topic"],
])
def test_marker_is_cwd_project_for_cross_project_check(tmp_path, monkeypatch, command):
    base = tmp_path / "crew"
    base.mkdir()
    repo = _repo(tmp_path / "repo")
    _state(base, "alfred", repo_path=str(repo))
    _state(base, "quota-ops")
    instance = repo / "instances" / "Quota"
    instance.mkdir(parents=True)
    (instance / ".crew-project").write_text("quota-ops\n")
    monkeypatch.chdir(instance)
    args = [*command, "--project", "alfred", "--base", str(base)]
    denied = CliRunner().invoke(crew, args)
    assert denied.exit_code != 0 and "--allow-cross-project" in denied.output
    with patch("agent_crew.cli._read_state", side_effect=RuntimeError("selected project")) as read:
        allowed = CliRunner().invoke(crew, [*args, "--allow-cross-project"])
    assert isinstance(allowed.exception, RuntimeError)
    assert "Cross-project" in allowed.output
    assert read.call_args.args == (str(base), "alfred")


def test_repo_root_and_crew_worktree_still_resolve(tmp_path, monkeypatch):
    base = tmp_path / "crew"
    base.mkdir()
    repo = _repo(tmp_path / "repo")
    subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty", "-qm", "init"],
                   check=True, env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
                                    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com"})
    worktree = tmp_path / "crew-worker"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(worktree)], check=True)
    _state(base, "alfred", repo_path=str(repo), worktrees={"codex": str(worktree)})
    monkeypatch.chdir(repo)
    assert _auto_detect_project(str(base)) == "alfred"
    monkeypatch.chdir(worktree)
    assert _auto_detect_project(str(base)) == "alfred"


def test_unmarked_instance_allows_explicit_project(tmp_path, monkeypatch):
    base = tmp_path / "crew"
    base.mkdir()
    repo = _repo(tmp_path / "repo")
    _state(base, "alfred", repo_path=str(repo))
    instance = repo / "instances" / "bot"
    instance.mkdir(parents=True)
    monkeypatch.chdir(instance)
    with patch("agent_crew.cli._read_state", side_effect=RuntimeError("selected project")) as read:
        result = CliRunner().invoke(crew, ["run", "task", "--project", "alfred", "--base", str(base)])
    assert isinstance(result.exception, RuntimeError)
    assert read.call_args.args == (str(base), "alfred")
