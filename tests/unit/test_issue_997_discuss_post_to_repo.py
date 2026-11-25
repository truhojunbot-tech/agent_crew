"""Issue #997: `crew discuss --project X --post-to N` resolved the target repo
with `get_repo()` and no `cwd`, i.e. from the caller's working directory
rather than from project X. On 2026-11-05 an apify-forge synthesis landed on
alfred#150 and the alpha_engine post failed with "no repo detected"; the same
thing recurred on 11-19 (alfred PR 342).

Now the repo comes from project X's registered worktrees (`state.json`
`worktrees`, then `repo_path`) via `get_repo(cwd=...)`, `OWNER/REPO#N` is
accepted explicitly, and an unresolvable repo aborts before any panel task is
enqueued or any gh call is made. The posted text also stops carrying the
fabricated "Proceed as discussed." decision.
"""
import json
import pathlib
import subprocess
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from agent_crew.cli import _discuss_post_repo, _parse_post_to, crew
from agent_crew.discussion import DISCUSS_NO_DECISION, enqueue_panel_tasks
from agent_crew.github import post_discussion_comment as _real_post_discussion_comment
from agent_crew.protocol import TaskResult
from agent_crew.queue import TaskQueue


def _git_repo(path: pathlib.Path, origin: str) -> pathlib.Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return path


def _gh_recorder(calls: list) -> callable:
    """`subprocess.run` stand-in that records `gh ...` invocations and passes
    everything else (the CLI's own git calls) through to the real function.
    Patching `subprocess.run` wholesale would also stub the git calls that
    `_matching_projects` and `get_repo` rely on.

    The CLI tests that reach the post request the `github_writes` fixture and
    put the real `post_discussion_comment` back: the conftest guard replaces
    it with a stub that never builds a gh argv, but the `--repo` on that argv
    is exactly what #997 is about. Nothing reaches the network — every `gh`
    argv stops here."""
    real_run = subprocess.run

    def run(args, *a, **kw):
        if isinstance(args, (list, tuple)) and args and args[0] == "gh":
            calls.append(list(args))
            return MagicMock(returncode=0, stdout="", stderr="")
        return real_run(args, *a, **kw)

    return run


def _stage_completed_round(db_path: str, topic: str = "AI strategy") -> list[str]:
    queue = TaskQueue(db_path)
    task_ids = enqueue_panel_tasks(
        queue, ["analyst", "critic"], topic, {"round": 1, "risk_tier": 1}, port=0
    )
    for tid, summary in zip(task_ids, ("analyst take.", "critic take.")):
        queue.submit_result(tid, TaskResult(task_id=tid, status="completed", summary=summary))
    return task_ids


def _project(base: pathlib.Path, name: str, **fields) -> dict:
    """Register a project under BASE with a pre-staged, completed discuss round."""
    state_dir = base / name
    state_dir.mkdir(parents=True)
    db = str(state_dir / "tasks.db")
    state = {"project": name, "db": db, "agents": ["analyst", "critic"],
             "pane_map": {"analyst": "%1", "critic": "%2"}, **fields}
    (state_dir / "state.json").write_text(json.dumps(state))
    return {"state": state, "db": db, "task_ids": _stage_completed_round(db)}


class TestParsePostTo:
    def test_u997_empty_means_no_post(self):
        assert _parse_post_to("") == ("", 0)
        assert _parse_post_to("  ") == ("", 0)

    def test_u997_bare_number_has_no_repo(self):
        assert _parse_post_to("42") == ("", 42)

    def test_u997_owner_repo_hash_number(self):
        assert _parse_post_to("truhojun/alpha_engine#5599") == ("truhojun/alpha_engine", 5599)

    @pytest.mark.parametrize("raw", ["abc", "0", "owner#3", "owner/repo", "owner/repo#", "-3"])
    def test_u997_rejects_garbage(self, raw):
        with pytest.raises(click.UsageError):
            _parse_post_to(raw)


class TestDiscussPostRepo:
    def test_u997_resolves_from_first_worktree_with_origin(self, tmp_path, monkeypatch):
        wt = _git_repo(tmp_path / "wt", "git@github.com:acme/projx.git")
        other = _git_repo(tmp_path / "cwd", "https://github.com/acme/other.git")
        monkeypatch.chdir(other)
        state = {"worktrees": {"claude": str(tmp_path / "gone"), "codex": str(wt)}}
        assert _discuss_post_repo("projx", state) == "acme/projx"

    def test_u997_falls_back_to_repo_path_not_cwd(self, tmp_path, monkeypatch):
        repo_path = _git_repo(tmp_path / "repo", "git@github.com:acme/projx.git")
        other = _git_repo(tmp_path / "cwd", "https://github.com/acme/other.git")
        monkeypatch.chdir(other)
        assert _discuss_post_repo("projx", {"repo_path": str(repo_path)}) == "acme/projx"

    def test_u997_missing_worktree_raises_without_cwd_fallback(self, tmp_path, monkeypatch):
        other = _git_repo(tmp_path / "cwd", "https://github.com/acme/other.git")
        monkeypatch.chdir(other)
        with pytest.raises(click.ClickException) as exc:
            _discuss_post_repo("projx", {"worktrees": {"claude": str(tmp_path / "gone")}})
        assert "projx" in str(exc.value)
        assert "acme/other" not in str(exc.value)
        with pytest.raises(click.ClickException):
            _discuss_post_repo("projx", None)


class TestDiscussPostToCli:
    def test_u997_posts_to_project_repo_not_cwd_repo(self, tmp_path, monkeypatch, github_writes):
        """Acceptance 1: run from a cwd inside a different repo; `gh issue
        comment` must carry `--repo <X's repo>`."""
        base = tmp_path / "base"
        wt = _git_repo(tmp_path / "wt", "git@github.com:acme/projx.git")
        other = _git_repo(tmp_path / "cwd", "https://github.com/acme/other.git")
        monkeypatch.chdir(other)
        proj = _project(base, "projx", worktrees={"claude": str(wt)})
        output = str(tmp_path / "synthesis.md")

        gh_calls: list = []
        with patch("agent_crew.discussion.enqueue_panel_tasks", return_value=proj["task_ids"]), \
             patch("agent_crew.cli._sync_worktrees_to_main", return_value={}), \
             patch("agent_crew.github.check_gh_installed", return_value=True), \
             patch("agent_crew.github.post_discussion_comment", _real_post_discussion_comment), \
             patch("agent_crew.github.subprocess.run", side_effect=_gh_recorder(gh_calls)):
            result = CliRunner().invoke(crew, [
                "discuss", "AI strategy", "--project", "projx", "--allow-cross-project",
                "--base", str(base), "--agents", "analyst,critic", "--risk-tier", "1",
                "--output", output, "--post-to", "7",
            ])

        assert result.exit_code == 0, result.output
        assert len(gh_calls) == 1, gh_calls
        args = gh_calls[0]
        assert args[:4] == ["gh", "issue", "comment", "7"]
        assert args[args.index("--repo") + 1] == "acme/projx"
        assert "acme/other" not in args
        assert "posted to issue acme/projx#7" in result.output

    def test_u997_missing_worktree_aborts_before_enqueue_and_gh(self, tmp_path, monkeypatch):
        """Acceptance 2: no worktree resolves -> clear error, no gh call. Also no
        panel task is enqueued, so no provider call is spent on an undeliverable
        discussion."""
        base = tmp_path / "base"
        other = _git_repo(tmp_path / "cwd", "https://github.com/acme/other.git")
        monkeypatch.chdir(other)
        _project(base, "projx", worktrees={"claude": str(tmp_path / "gone")})
        output = str(tmp_path / "synthesis.md")

        gh_calls: list = []
        with patch("agent_crew.discussion.enqueue_panel_tasks") as enqueue, \
             patch("agent_crew.cli._sync_worktrees_to_main", return_value={}), \
             patch("agent_crew.github.check_gh_installed", return_value=True), \
             patch("agent_crew.github.post_discussion_comment", _real_post_discussion_comment), \
             patch("agent_crew.github.subprocess.run", side_effect=_gh_recorder(gh_calls)):
            result = CliRunner().invoke(crew, [
                "discuss", "AI strategy", "--project", "projx", "--allow-cross-project",
                "--base", str(base), "--agents", "analyst,critic", "--risk-tier", "1",
                "--output", output, "--post-to", "7",
            ])

        assert result.exit_code != 0
        assert "cannot resolve the GitHub repo for project 'projx'" in result.output
        assert gh_calls == []
        enqueue.assert_not_called()
        assert not pathlib.Path(output).exists()

    def test_u997_project_without_worktrees_never_uses_cwd(self, tmp_path, monkeypatch):
        """A state.json with no worktrees at all must not quietly post to the
        caller's cwd repo (that is exactly the 11-05 alfred#150 misfire)."""
        base = tmp_path / "base"
        other = _git_repo(tmp_path / "cwd", "https://github.com/acme/other.git")
        monkeypatch.chdir(other)
        _project(base, "projx")

        gh_calls: list = []
        with patch("agent_crew.discussion.enqueue_panel_tasks") as enqueue, \
             patch("agent_crew.github.subprocess.run", side_effect=_gh_recorder(gh_calls)):
            result = CliRunner().invoke(crew, [
                "discuss", "AI strategy", "--project", "projx", "--allow-cross-project",
                "--base", str(base), "--agents", "analyst,critic", "--risk-tier", "1",
                "--output", str(tmp_path / "s.md"), "--post-to", "7",
            ])

        assert result.exit_code != 0
        assert "none registered" in result.output
        assert gh_calls == []
        enqueue.assert_not_called()

    def test_u997_explicit_owner_repo_wins(self, tmp_path, monkeypatch, github_writes):
        """OWNER/REPO#N is honoured verbatim, even when the project's worktree
        would resolve to something else."""
        base = tmp_path / "base"
        wt = _git_repo(tmp_path / "wt", "git@github.com:acme/projx.git")
        monkeypatch.chdir(wt)
        proj = _project(base, "projx", worktrees={"claude": str(wt)})
        output = str(tmp_path / "synthesis.md")

        gh_calls: list = []
        with patch("agent_crew.discussion.enqueue_panel_tasks", return_value=proj["task_ids"]), \
             patch("agent_crew.cli._sync_worktrees_to_main", return_value={}), \
             patch("agent_crew.github.check_gh_installed", return_value=True), \
             patch("agent_crew.github.post_discussion_comment", _real_post_discussion_comment), \
             patch("agent_crew.github.subprocess.run", side_effect=_gh_recorder(gh_calls)):
            result = CliRunner().invoke(crew, [
                "discuss", "AI strategy", "--project", "projx",
                "--base", str(base), "--agents", "analyst,critic", "--risk-tier", "1",
                "--output", output, "--post-to", "truhojun/alpha_engine#5599",
            ])

        assert result.exit_code == 0, result.output
        assert len(gh_calls) == 1, gh_calls
        args = gh_calls[0]
        assert args[:4] == ["gh", "issue", "comment", "5599"]
        assert args[args.index("--repo") + 1] == "truhojun/alpha_engine"

    def test_u997_bad_post_to_is_a_usage_error(self, tmp_path):
        result = CliRunner().invoke(crew, [
            "discuss", "AI strategy", "--db", str(tmp_path / "tasks.db"),
            "--risk-tier", "1", "--post-to", "not-an-issue",
        ])
        assert result.exit_code == 2
        assert "OWNER/REPO#N" in result.output


class TestNoFabricatedDecision:
    def test_u997_synthesis_has_no_proceed_as_discussed(self, tmp_path):
        """Acceptance 3: the output carries no 'Proceed as discussed.' line."""
        db_path = str(tmp_path / "tasks.db")
        output = str(tmp_path / "synthesis.md")
        task_ids = _stage_completed_round(db_path)

        with patch("agent_crew.discussion.enqueue_panel_tasks", return_value=task_ids), \
             patch("agent_crew.github.post_discussion_comment", return_value=True) as mock_post:
            result = CliRunner().invoke(crew, [
                "discuss", "AI strategy", "--db", db_path, "--agents", "analyst,critic",
                "--risk-tier", "1", "--output", output, "--post-to", "42",
            ])

        assert result.exit_code == 0, result.output
        text = pathlib.Path(output).read_text()
        assert "Proceed as discussed." not in text
        assert "## Decision\n" + DISCUSS_NO_DECISION in text
        assert "No synthesizer ran" in text
        assert "analyst take." in text and "critic take." in text
        # The posted body is the same text, so it carries no fabricated decision either.
        posted = mock_post.call_args[0][2]
        assert "Proceed as discussed." not in posted
        assert DISCUSS_NO_DECISION in posted
        # Standalone --db mode (no --project) keeps the pre-#997 repo fallback.
        assert mock_post.call_args.kwargs.get("repo") is None
