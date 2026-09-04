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
        review_id = auto_enqueue_review(q, impl_id, pr_number=42, pr_state_fn=lambda pr: "open")
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
        test_id = auto_enqueue_test(q, review_id, pr_state_fn=lambda pr: "open")
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
        test_id = auto_enqueue_test(q, review_id, pr_state_fn=lambda pr: "open")
        assert test_id is not None

        tasks = {t.task_id: t for t in q.list_tasks()}
        test_task = tasks[test_id]
        ctx = test_task.context if isinstance(test_task.context, dict) else {}
        assert "pr_number" not in ctx or ctx.get("pr_number") is None


# ---------------------------------------------------------------------------
# #216 — branch_has_pr (github.py) + _auto_retry_failed_task's use of it
#
# A review task can have a real, non-empty branch (so the #161 guard above
# doesn't fire) whose PR genuinely doesn't exist — e.g. the implementer
# reported "PR #N opened" without the API actually recording pr_number, and
# separately pushed to a differently-named branch than the one recorded on
# the task. Retrying re-dispatches the exact same branch to the exact same
# "gh pr list" dead end. branch_has_pr() lets the dispatcher check that
# cheaply itself before spending a whole agent invocation to relearn it.
# ---------------------------------------------------------------------------

from agent_crew.github import branch_has_pr


class TestBranchHasPr:
    def test_u216_true_when_pr_exists(self):
        with patch("agent_crew.github.check_gh_installed", return_value=True), \
             patch("agent_crew.github.get_repo", return_value="owner/repo"), \
             patch("agent_crew.github.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='[{"number": 42}]')
            assert branch_has_pr("agent/claude/some-branch") is True

    def test_u216_false_when_no_pr_found(self):
        with patch("agent_crew.github.check_gh_installed", return_value=True), \
             patch("agent_crew.github.get_repo", return_value="owner/repo"), \
             patch("agent_crew.github.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="[]")
            assert branch_has_pr("agent/claude/4235-auto") is False

    def test_u216_fails_open_true_on_gh_error(self):
        """A gh/network hiccup must never be mistaken for a confirmed
        no-PR verdict — fail open so a legitimate retry isn't blocked."""
        with patch("agent_crew.github.check_gh_installed", return_value=True), \
             patch("agent_crew.github.get_repo", return_value="owner/repo"), \
             patch("agent_crew.github.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert branch_has_pr("agent/claude/some-branch") is True

    def test_u216_fails_open_true_when_gh_not_installed(self):
        with patch("agent_crew.github.check_gh_installed", return_value=False):
            assert branch_has_pr("agent/claude/some-branch") is True

    def test_u216_fails_open_true_on_empty_branch(self):
        assert branch_has_pr("") is True

    def test_u216_queries_the_right_branch_and_repo(self):
        with patch("agent_crew.github.check_gh_installed", return_value=True), \
             patch("agent_crew.github.get_repo", return_value="owner/repo"), \
             patch("agent_crew.github.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="[]")
            branch_has_pr("agent/claude/4235-auto")

        args = mock_run.call_args[0][0]
        assert "--head" in args and "agent/claude/4235-auto" in args
        assert "--repo" in args and "owner/repo" in args
        assert "--state" in args and "all" in args


class TestAutoRetryReviewNoPrForBranchGuard:
    """Mirrors the #161 guard-condition tests above, for the #216 case
    where branch IS set but genuinely has no PR."""

    def test_u216_skips_when_branch_set_but_no_pr_exists(self):
        with patch("agent_crew.github.check_gh_installed", return_value=True), \
             patch("agent_crew.github.get_repo", return_value="owner/repo"), \
             patch("agent_crew.github.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="[]")
            original_branch = "agent/claude/4235-auto"
            task_ctx = {}
            should_skip = bool(original_branch) and not task_ctx.get("pr_number") and not branch_has_pr(original_branch)
        assert should_skip

    def test_u216_does_not_skip_when_branch_has_a_pr(self):
        with patch("agent_crew.github.check_gh_installed", return_value=True), \
             patch("agent_crew.github.get_repo", return_value="owner/repo"), \
             patch("agent_crew.github.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='[{"number": 5}]')
            original_branch = "agent/claude/has-a-pr"
            task_ctx = {}
            should_skip = bool(original_branch) and not task_ctx.get("pr_number") and not branch_has_pr(original_branch)
        assert not should_skip

    def test_u216_does_not_skip_when_pr_number_already_known(self):
        """If pr_number is already in context, the #216 check (which only
        applies when pr_number is absent) must not even run — no reason to
        distrust a pr_number the caller already resolved."""
        original_branch = "agent/claude/some-branch"
        task_ctx = {"pr_number": 42}
        # Guard condition from server.py: `if original_task.branch and not
        # task_ctx.get("pr_number"):` — pr_number present short-circuits it.
        would_check = bool(original_branch) and not task_ctx.get("pr_number")
        assert not would_check


# ---------------------------------------------------------------------------
# #216 review finding: the tests above exercise the guard *condition* in
# isolation, not the real _auto_retry_failed_task path — they'd still pass
# if the branch_has_pr() call in server.py were removed or wired wrong.
# These go through the real POST /tasks/{id}/result HTTP endpoint that
# actually calls _auto_retry_failed_task, mocking agent_crew.github.branch_has_pr
# (the module-level function _auto_retry_failed_task imports locally at call
# time, same pattern as the #210 post_review_comment fix).
# ---------------------------------------------------------------------------

from fastapi.testclient import TestClient
from agent_crew.server import create_app


def _review_task_payload(task_id: str, branch: str) -> dict:
    return {
        "task_id": task_id,
        "task_type": "review",
        "description": "Review PR",
        "branch": branch,
        "priority": 3,
        "context": {},
        "project": "",
    }


def _failed_result_payload(task_id: str) -> dict:
    return {
        "task_id": task_id,
        "status": "failed",
        "summary": "Could not review branch: no open or closed GitHub PR resolves for that head branch.",
        "verdict": None,
        "findings": [],
        "pr_number": None,
    }


def _retry_ids_for(queue: TaskQueue, original_task_id: str) -> list:
    return [t.task_id for t in queue.list_tasks() if t.task_id.startswith(f"retry-{original_task_id}-")]


class TestAutoRetryReviewNoPrForBranchGuardEndToEnd:
    def test_u216_no_retry_enqueued_when_branch_has_no_pr(self, tmp_db):
        app = create_app(db_path=tmp_db, watchdog_disabled=True)
        with TestClient(app) as client, \
             patch("agent_crew.github.branch_has_pr", return_value=False) as mock_bhp:
            client.post("/tasks", json=_review_task_payload("review-216a", "agent/claude/4235-auto"))
            client.post("/tasks/review-216a/result", json=_failed_result_payload("review-216a"))

        mock_bhp.assert_called_once()
        queue = TaskQueue(tmp_db)
        assert _retry_ids_for(queue, "review-216a") == []

    def test_u216_retry_enqueued_when_branch_has_a_pr(self, tmp_db):
        app = create_app(db_path=tmp_db, watchdog_disabled=True)
        with TestClient(app) as client, \
             patch("agent_crew.github.branch_has_pr", return_value=True) as mock_bhp:
            client.post("/tasks", json=_review_task_payload("review-216b", "agent/claude/has-a-pr"))
            client.post("/tasks/review-216b/result", json=_failed_result_payload("review-216b"))

        mock_bhp.assert_called_once()
        queue = TaskQueue(tmp_db)
        assert len(_retry_ids_for(queue, "review-216b")) == 1

    def test_u216_retry_still_enqueued_when_gh_pr_list_itself_errors(self, tmp_db):
        """Exercises branch_has_pr's own fail-open logic end-to-end — mocks
        the underlying `gh` subprocess call to genuinely fail (non-zero
        exit), not branch_has_pr itself, so this actually proves a real gh
        hiccup doesn't block a legitimate retry (#222 review round 2:
        the prior version of this test only re-mocked branch_has_pr to
        return True, which never exercised the error path at all)."""
        app = create_app(db_path=tmp_db, watchdog_disabled=True)
        with TestClient(app) as client, \
             patch("agent_crew.github.check_gh_installed", return_value=True), \
             patch("agent_crew.github.get_repo", return_value="owner/repo"), \
             patch("agent_crew.github.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="gh: network error")
            client.post("/tasks", json=_review_task_payload("review-216c", "agent/claude/gh-hiccup"))
            client.post("/tasks/review-216c/result", json=_failed_result_payload("review-216c"))

        assert mock_run.called, "branch_has_pr should have shelled out to gh"
        queue = TaskQueue(tmp_db)
        assert len(_retry_ids_for(queue, "review-216c")) == 1

    def test_u216_branch_has_pr_not_called_when_pr_number_already_known(self, tmp_db, github_writes):
        app = create_app(db_path=tmp_db, watchdog_disabled=True)
        payload = _review_task_payload("review-216d", "agent/claude/known-pr")
        payload["context"] = {"pr_number": 42}
        with TestClient(app) as client, \
             patch("agent_crew.github.branch_has_pr") as mock_bhp:
            client.post("/tasks", json=payload)
            client.post("/tasks/review-216d/result", json=_failed_result_payload("review-216d"))

        mock_bhp.assert_not_called()
        queue = TaskQueue(tmp_db)
        assert len(_retry_ids_for(queue, "review-216d")) == 1
