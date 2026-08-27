"""Issue #219: `crew discuss` had no active result-delivery path — the
synthesis only ever landed in a local file (--output, default
synthesis.md). A caller that wasn't the terminal that ran it (a bot
invoking it as a subprocess, a scheduled job) had no way to actually
receive the result short of separately reading that file, which combined
with #213 (GET /tasks dropping result fields) meant discuss results were
effectively unreachable for automated callers.

Adds `post_discussion_comment(issue_number, topic, synthesis, repo=None)`
to github.py (same shape/conventions as the existing post_review_comment)
and a `--post-to <issue>` option on `crew discuss` that calls it once the
synthesis is built.
"""
from unittest.mock import MagicMock, patch

import pathlib
from click.testing import CliRunner

from agent_crew.cli import crew
from agent_crew.github import post_discussion_comment
from agent_crew.discussion import enqueue_panel_tasks
from agent_crew.protocol import TaskResult
from agent_crew.queue import TaskQueue


class TestPostDiscussionComment:
    def test_u219_posts_comment_with_topic_and_synthesis(self):
        with patch("agent_crew.github.check_gh_installed", return_value=True), \
             patch("agent_crew.github.get_repo", return_value="owner/repo"), \
             patch("agent_crew.github.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = post_discussion_comment(42, "Adopt Rust?", "## Synthesis\n\nDo it.")

        assert result is True
        args = mock_run.call_args[0][0]
        assert args[:3] == ["gh", "issue", "comment"]
        assert "42" in args
        assert "--repo" in args and "owner/repo" in args
        body = args[args.index("--body") + 1]
        assert "Adopt Rust?" in body
        assert "Do it." in body

    def test_u219_returns_false_on_gh_failure(self):
        with patch("agent_crew.github.check_gh_installed", return_value=True), \
             patch("agent_crew.github.get_repo", return_value="owner/repo"), \
             patch("agent_crew.github.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert post_discussion_comment(42, "topic", "synthesis") is False

    def test_u219_returns_false_when_gh_not_installed(self):
        with patch("agent_crew.github.check_gh_installed", return_value=False):
            assert post_discussion_comment(42, "topic", "synthesis") is False

    def test_u219_returns_false_when_no_repo(self):
        with patch("agent_crew.github.check_gh_installed", return_value=True), \
             patch("agent_crew.github.get_repo", return_value=None):
            assert post_discussion_comment(42, "topic", "synthesis") is False


def _make_completed_discuss(db_path: str, output: str, topic: str = "AI strategy"):
    """Pre-stage a discuss round where both panelists have already
    responded, so `crew discuss` (run synchronously, no --nowait) finds
    both immediately without needing to actually wait/timeout."""
    queue = TaskQueue(db_path)
    task_ids = enqueue_panel_tasks(
        queue, ["analyst", "critic"], topic, {"round": 1}, port=0
    )
    queue.submit_result(task_ids[0], TaskResult(
        task_id=task_ids[0], status="completed", summary="analyst take.",
    ))
    queue.submit_result(task_ids[1], TaskResult(
        task_id=task_ids[1], status="completed", summary="critic take.",
    ))
    return task_ids


class TestDiscussPostToOption:
    def test_u219_post_to_posts_synthesis_on_completion(self, tmp_path):
        db_path = str(tmp_path / "tasks.db")
        output = str(tmp_path / "synthesis.md")
        task_ids = _make_completed_discuss(db_path, output)

        runner = CliRunner()
        with patch("agent_crew.discussion.enqueue_panel_tasks", return_value=task_ids), \
             patch("agent_crew.github.post_discussion_comment", return_value=True) as mock_post:
            result = runner.invoke(crew, [
                "discuss", "AI strategy",
                "--db", db_path,
                "--agents", "analyst,critic",
                "--output", output,
                "--post-to", "42",
            ])

        assert result.exit_code == 0, result.output
        assert "posted to issue #42" in result.output
        mock_post.assert_called_once()
        call_args = mock_post.call_args[0]
        assert call_args[0] == 42
        assert call_args[1] == "AI strategy"
        assert "analyst take." in call_args[2] or "critic take." in call_args[2]

    def test_u219_no_post_to_means_no_post_call(self, tmp_path):
        """Default behavior (no --post-to) must not attempt any GitHub call —
        purely additive, no change for existing callers."""
        db_path = str(tmp_path / "tasks.db")
        output = str(tmp_path / "synthesis.md")
        task_ids = _make_completed_discuss(db_path, output)

        runner = CliRunner()
        with patch("agent_crew.discussion.enqueue_panel_tasks", return_value=task_ids), \
             patch("agent_crew.github.post_discussion_comment") as mock_post:
            result = runner.invoke(crew, [
                "discuss", "AI strategy",
                "--db", db_path,
                "--agents", "analyst,critic",
                "--output", output,
            ])

        assert result.exit_code == 0, result.output
        mock_post.assert_not_called()

    def test_u219_post_to_failure_warns_but_does_not_fail_the_command(self, tmp_path):
        """gh being unavailable/erroring must not turn a successful
        discussion into a failed command — the synthesis file is still
        there either way."""
        db_path = str(tmp_path / "tasks.db")
        output = str(tmp_path / "synthesis.md")
        task_ids = _make_completed_discuss(db_path, output)

        runner = CliRunner()
        with patch("agent_crew.discussion.enqueue_panel_tasks", return_value=task_ids), \
             patch("agent_crew.github.post_discussion_comment", return_value=False):
            result = runner.invoke(crew, [
                "discuss", "AI strategy",
                "--db", db_path,
                "--agents", "analyst,critic",
                "--output", output,
                "--post-to", "42",
            ])

        assert result.exit_code == 0, result.output
        assert "failed to post synthesis" in result.output.lower()
        assert pathlib.Path(output).exists()

    def test_u219_nowait_ignores_post_to(self, tmp_path):
        """--nowait returns before any synthesis exists — --post-to must not
        be able to post an empty/nonexistent synthesis."""
        db_path = str(tmp_path / "tasks.db")

        runner = CliRunner()
        with patch("agent_crew.github.post_discussion_comment") as mock_post:
            result = runner.invoke(crew, [
                "discuss", "AI strategy",
                "--db", db_path,
                "--agents", "analyst,critic",
                "--nowait",
                "--post-to", "42",
            ])

        assert result.exit_code == 0, result.output
        mock_post.assert_not_called()
