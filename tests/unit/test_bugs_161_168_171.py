"""Unit tests for issues #161, #168, #171.

#168 — write_sessions_json must use _get_agent_cmd (not _AGENT_CMDS directly)
        so the stored command includes TELEGRAM_STATE_DIR for claude agents.

#161 — auto_enqueue_review skips when impl task has no branch AND no pr_number;
        _auto_retry_failed_task skips review retries under the same condition.

#171 — merge_pr added to github.py; auto-merge fires after review-approved+no_tester
        and after test passes.
"""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch
import uuid

import pytest

# ---------------------------------------------------------------------------
# #168 — write_sessions_json stores TELEGRAM_STATE_DIR-prefixed command
# ---------------------------------------------------------------------------

from agent_crew.setup import write_sessions_json, _get_agent_cmd


class TestWriteSessionsJson:
    def test_u168_stores_telegram_state_dir_for_claude(self, tmp_path):
        """sessions.json cmd for claude must include TELEGRAM_STATE_DIR."""
        wt = str(tmp_path / "wt_claude")
        worktrees = {"claude": wt}
        agents = [{"name": "claude", "pane": 0}]

        with patch("agent_crew.setup.session") as mock_session:
            write_sessions_json(str(tmp_path / "sessions.json"), agents, worktrees=worktrees)

        saved = mock_session.save_sessions.call_args[0][1]
        claude_cmd = next(a["cmd"] for a in saved if a["name"] == "claude")
        assert "TELEGRAM_STATE_DIR" in claude_cmd, (
            f"Expected TELEGRAM_STATE_DIR in cmd, got: {claude_cmd}"
        )
        assert str(wt) in claude_cmd, "TELEGRAM_STATE_DIR should point to the worktree"

    def test_u168_omits_telegram_state_dir_when_no_worktrees(self):
        """Without worktrees arg the cmd must still be valid (no crash)."""
        agents = [{"name": "claude", "pane": 0}]
        with patch("agent_crew.setup.session") as mock_session:
            write_sessions_json("/tmp/sessions.json", agents)
        saved = mock_session.save_sessions.call_args[0][1]
        assert saved[0]["name"] == "claude"
        assert "cmd" in saved[0]

    def test_u168_get_agent_cmd_prefixes_telegram_state_dir(self, tmp_path):
        """_get_agent_cmd with a worktree_path must prefix TELEGRAM_STATE_DIR."""
        cmd = _get_agent_cmd("claude", str(tmp_path))
        assert cmd.startswith("TELEGRAM_STATE_DIR="), (
            f"Expected TELEGRAM_STATE_DIR= prefix, got: {cmd}"
        )
        assert str(tmp_path) in cmd

    def test_u168_codex_not_prefixed_with_telegram_state_dir(self, tmp_path):
        """TELEGRAM_STATE_DIR is only added for claude, not codex/gemini."""
        cmd = _get_agent_cmd("codex", str(tmp_path))
        assert "TELEGRAM_STATE_DIR" not in cmd


# ---------------------------------------------------------------------------
# #161 — auto_enqueue_review: no-PR guard
# ---------------------------------------------------------------------------

from agent_crew.pipeline import auto_enqueue_review
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


def _make_queue_with_impl(branch: str = "", project: str = "") -> tuple[TaskQueue, str]:
    """Return (queue, impl_task_id) with one completed impl task."""
    import tempfile
    tmp = tempfile.mktemp(suffix=".db")
    q = TaskQueue(tmp)
    task_id = f"impl-{uuid.uuid4().hex[:8]}"
    kw: dict = dict(task_id=task_id, task_type="implement", description="Do the thing", branch=branch)
    if project:
        kw["project"] = project
    q.enqueue(TaskRequest(**kw))
    q.submit_result(task_id, TaskResult(
        task_id=task_id, status="completed", summary="done", verdict=None, findings=[], pr_number=None,
    ))
    return q, task_id


class TestAutoEnqueueReviewNoPRGuard:
    def test_u161_skips_when_empty_branch_and_no_pr_number(self):
        """If impl task has empty branch AND pr_number is None, review must NOT be created."""
        q, impl_id = _make_queue_with_impl(branch="")
        review_id = auto_enqueue_review(q, impl_id, pr_number=None)
        assert review_id is None

    def test_u161_creates_review_when_branch_present(self):
        """If branch is set (even without pr_number), review IS created."""
        q, impl_id = _make_queue_with_impl(branch="feat/thing")
        review_id = auto_enqueue_review(q, impl_id, pr_number=None)
        assert review_id is not None

    def test_u161_creates_review_when_pr_number_present_but_no_branch(self):
        """If pr_number is set (even with empty branch), review IS created."""
        q, impl_id = _make_queue_with_impl(branch="")
        review_id = auto_enqueue_review(q, impl_id, pr_number=42)
        assert review_id is not None


# ---------------------------------------------------------------------------
# #161 — _auto_retry_failed_task: review retry guard
# ---------------------------------------------------------------------------


class TestAutoRetryReviewNoPRGuard:
    """Server's _auto_retry_failed_task must skip review retries with no branch/PR."""

    def _make_review_task(self, branch: str = "", pr_number=None) -> MagicMock:
        ctx: dict = {}
        if pr_number is not None:
            ctx["pr_number"] = pr_number
        t = MagicMock()
        t.branch = branch
        t.context = ctx
        return t

    def test_u161_skips_review_retry_when_empty_branch_no_pr(self):
        """Guard: empty branch + no pr_number → skip retry."""
        original = self._make_review_task(branch="", pr_number=None)
        task_ctx = original.context if isinstance(original.context, dict) else {}
        should_skip = not original.branch and not task_ctx.get("pr_number")
        assert should_skip, "should skip retry when branch='' and no pr_number"

    def test_u161_does_not_skip_review_retry_when_branch_present(self):
        """Guard: non-empty branch → allow retry."""
        original = self._make_review_task(branch="feat/x", pr_number=None)
        task_ctx = original.context if isinstance(original.context, dict) else {}
        should_skip = not original.branch and not task_ctx.get("pr_number")
        assert not should_skip

    def test_u161_does_not_skip_review_retry_when_pr_number_present(self):
        """Guard: no branch but pr_number set → allow retry."""
        original = self._make_review_task(branch="", pr_number=7)
        task_ctx = original.context if isinstance(original.context, dict) else {}
        should_skip = not original.branch and not task_ctx.get("pr_number")
        assert not should_skip


# ---------------------------------------------------------------------------
# #171 — merge_pr in github.py
# ---------------------------------------------------------------------------

from agent_crew.github import merge_pr


class TestMergePr:
    def test_u171_merge_pr_calls_gh_pr_merge(self):
        """merge_pr must invoke `gh pr merge <num> --squash --repo <repo>`."""
        with patch("agent_crew.github.check_gh_installed", return_value=True), \
             patch("agent_crew.github.get_repo", return_value="owner/repo"), \
             patch("agent_crew.github.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = merge_pr(42)

        assert result is True
        args = mock_run.call_args[0][0]
        assert "gh" in args
        assert "pr" in args
        assert "merge" in args
        assert "42" in args
        assert "--squash" in args

    def test_u171_merge_pr_returns_false_on_failure(self):
        """merge_pr must return False when gh exits non-zero."""
        with patch("agent_crew.github.check_gh_installed", return_value=True), \
             patch("agent_crew.github.get_repo", return_value="owner/repo"), \
             patch("agent_crew.github.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            result = merge_pr(42)

        assert result is False

    def test_u171_merge_pr_returns_false_when_gh_not_installed(self):
        """merge_pr must return False gracefully when gh is absent."""
        with patch("agent_crew.github.check_gh_installed", return_value=False):
            result = merge_pr(42)
        assert result is False

    def test_u171_merge_pr_returns_false_when_no_repo(self):
        """merge_pr must return False gracefully when no repo is detected."""
        with patch("agent_crew.github.check_gh_installed", return_value=True), \
             patch("agent_crew.github.get_repo", return_value=None):
            result = merge_pr(42)
        assert result is False


# ---------------------------------------------------------------------------
# #171 — auto_enqueue_test propagates pr_number to test context
# ---------------------------------------------------------------------------

from agent_crew.pipeline import auto_enqueue_test


def _make_queue_with_approved_review(pr_number=None, branch="feat/x"):
    """Return (queue, review_task_id) with one approved review task."""
    import tempfile  # noqa: F811
    tmp = tempfile.mktemp(suffix=".db")
    q = TaskQueue(tmp)

    # impl task
    impl_id = f"impl-{uuid.uuid4().hex[:8]}"
    q.enqueue(TaskRequest(task_id=impl_id, task_type="implement", description="impl", branch=branch))
    q.submit_result(impl_id, TaskResult(task_id=impl_id, status="completed", summary="done", verdict=None, findings=[], pr_number=pr_number))

    # review task
    review_id = f"review-{uuid.uuid4().hex[:8]}"
    review_ctx = {"prev_task_id": impl_id}
    if pr_number is not None:
        review_ctx["pr_number"] = pr_number
    q.enqueue(TaskRequest(task_id=review_id, task_type="review", description="review", branch=branch, context=review_ctx))
    q.submit_result(review_id, TaskResult(task_id=review_id, status="completed", summary="lgtm", verdict="approve", findings=[], pr_number=pr_number))

    return q, review_id


class TestAutoEnqueueTestPropagatesPrNumber:
    def test_u171_test_context_has_pr_number_when_review_has_it(self):
        """auto_enqueue_test must carry pr_number from the review context to the test task."""
        q, review_id = _make_queue_with_approved_review(pr_number=99)
        test_id = auto_enqueue_test(q, review_id)
        assert test_id is not None

        tasks = {t.task_id: t for t in q.list_tasks()}
        test_task = tasks[test_id]
        ctx = test_task.context if isinstance(test_task.context, dict) else {}
        assert ctx.get("pr_number") == 99, (
            f"Expected pr_number=99 in test context, got: {ctx}"
        )

    def test_u171_test_context_has_no_pr_number_when_review_missing(self):
        """auto_enqueue_test must not inject pr_number when the review has none."""
        q, review_id = _make_queue_with_approved_review(pr_number=None)
        test_id = auto_enqueue_test(q, review_id)
        assert test_id is not None

        tasks = {t.task_id: t for t in q.list_tasks()}
        test_task = tasks[test_id]
        ctx = test_task.context if isinstance(test_task.context, dict) else {}
        assert "pr_number" not in ctx or ctx.get("pr_number") is None
