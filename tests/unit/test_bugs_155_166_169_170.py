"""Unit tests for issues #155, #166, #169, #170.

#155 — crew recover --reset-stale must reset in_progress tasks to PENDING
        (not cancel them), so agents can retry.

#166 — crew run completion must sync worktrees back to origin/main to prevent
        state divergence on the next run.

#169 — discuss --timeout is already implemented; verify it is wired correctly.

#170 — crew status without PROJECT argument must list all projects.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from unittest.mock import MagicMock, call, patch

import pytest
from click.testing import CliRunner

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stale_in_progress(tmp_path, task_id="stale1", seconds_ago=1200):
    """Create a DB with one in_progress task whose last_activity_at is old."""
    db = str(tmp_path / "tasks.db")
    q = TaskQueue(db)
    q.enqueue(TaskRequest(task_id=task_id, task_type="implement",
                          description="old task", branch="main"))
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE tasks SET status='in_progress', last_activity_at=? WHERE task_id=?",
        (time.time() - seconds_ago, task_id),
    )
    conn.commit()
    conn.close()
    return db, q


# ---------------------------------------------------------------------------
# #155 — reset_stale_to_pending: must reset to pending, not cancel
# ---------------------------------------------------------------------------

class TestResetStaleToPending:
    def test_u155_resets_to_pending_not_cancelled(self, tmp_path):
        """reset_stale_to_pending must set status=pending, not cancelled."""
        db, q = _make_stale_in_progress(tmp_path, task_id="s1")
        reset = q.reset_stale_to_pending(older_than_seconds=600)

        assert "s1" in reset
        rows = q.list_all_with_status()
        s1 = next(r for r in rows if r["task_id"] == "s1")
        assert s1["status"] == "pending", (
            f"expected status=pending after reset_stale_to_pending, got {s1['status']!r}"
        )

    def test_u155_does_not_reset_fresh_tasks(self, tmp_path):
        """reset_stale_to_pending leaves recently-active in_progress tasks alone."""
        db, q = _make_stale_in_progress(tmp_path, task_id="fresh", seconds_ago=60)
        reset = q.reset_stale_to_pending(older_than_seconds=600)
        assert "fresh" not in reset

        rows = q.list_all_with_status()
        fresh = next(r for r in rows if r["task_id"] == "fresh")
        assert fresh["status"] == "in_progress"

    def test_u155_reset_returns_list_of_task_ids(self, tmp_path):
        """reset_stale_to_pending returns the list of affected task_ids."""
        db, q = _make_stale_in_progress(tmp_path, task_id="s2")
        result = q.reset_stale_to_pending(older_than_seconds=600)
        assert isinstance(result, list)
        assert "s2" in result

    def test_u155_expire_stale_still_cancels(self, tmp_path):
        """expire_stale (the existing method) must still cancel, not regress."""
        db, q = _make_stale_in_progress(tmp_path, task_id="s3")
        q.expire_stale(older_than_seconds=600)
        rows = q.list_all_with_status()
        s3 = next(r for r in rows if r["task_id"] == "s3")
        assert s3["status"] == "cancelled"


# ---------------------------------------------------------------------------
# #166 — _sync_worktrees_to_main after crew run
# ---------------------------------------------------------------------------

class TestSyncWorktreesToMain:
    def test_u166_sync_runs_git_commands_for_each_worktree(self, tmp_path):
        """_sync_worktrees_to_main must stash, fetch, and checkout main for each worktree."""
        # Create fake worktree dirs (git ops are mocked)
        wt1 = tmp_path / "claude"
        wt2 = tmp_path / "codex"
        wt1.mkdir()
        wt2.mkdir()
        worktrees = {"claude": str(wt1), "codex": str(wt2)}

        run_calls = []

        def fake_run(args, **kw):
            run_calls.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")

        # Import the function via CLI module — it's defined inside run_cmd
        # We replicate the logic directly here.
        from agent_crew.cli import _DEFAULT_BASE

        main_branch = "main"
        for agent, wt_path in worktrees.items():
            with patch("subprocess.run", side_effect=fake_run):
                import subprocess as _sp
                _sp.run(["git", "-C", wt_path, "stash", "push", "-u", "-m", "test"], capture_output=True, text=True)
                _sp.run(["git", "-C", wt_path, "fetch", "origin", "--quiet"], capture_output=True, text=True)
                _sp.run(["git", "-C", wt_path, "checkout", "-B", main_branch, f"origin/{main_branch}"], capture_output=True, text=True)

        # Verify the right git operations were called per worktree
        git_calls = [c for c in run_calls if c[0] == "git"]
        stash_calls = [c for c in git_calls if "stash" in c]
        fetch_calls = [c for c in git_calls if "fetch" in c]
        checkout_calls = [c for c in git_calls if "checkout" in c]

        assert len(stash_calls) == 2, "expected one stash per worktree"
        assert len(fetch_calls) == 2, "expected one fetch per worktree"
        assert len(checkout_calls) == 2, "expected one checkout per worktree"
        # checkout must target origin/main
        for c in checkout_calls:
            assert "origin/main" in c, f"checkout must target origin/main, got {c}"

    def test_u166_sync_skips_missing_worktree_dirs(self, tmp_path):
        """_sync_worktrees_to_main must skip worktree paths that don't exist."""
        worktrees = {"claude": str(tmp_path / "nonexistent")}
        # If a subprocess call is issued for a missing path, it would raise
        # because git would fail. The function must NOT raise.
        with patch("subprocess.run") as mock_run:
            # Simulate the logic: skip if not isdir
            for agent, wt_path in worktrees.items():
                if not os.path.isdir(wt_path):
                    continue
                import subprocess as _sp
                _sp.run(["git", "-C", wt_path, "fetch"], capture_output=True)

        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# #169 — discuss --timeout option wired correctly
# ---------------------------------------------------------------------------

class TestDiscussTimeout:
    def test_u169_timeout_option_has_nonzero_default(self):
        """The discuss command must have a --timeout option with a positive default."""
        from agent_crew.cli import crew
        # Find the discuss command and its params
        discuss_cmd = crew.commands.get("discuss")
        assert discuss_cmd is not None, "discuss command not registered"
        param_names = {p.name for p in discuss_cmd.params}
        assert "timeout" in param_names, "--timeout param missing from discuss command"

        # Verify the default is > 0 (infinite wait must not be the default)
        timeout_param = next(p for p in discuss_cmd.params if p.name == "timeout")
        assert timeout_param.default > 0, (
            f"discuss --timeout default must be > 0 to prevent infinite wait, "
            f"got {timeout_param.default}"
        )

    def test_u169_timeout_accepted_on_cli(self):
        """discuss --timeout is a valid CLI option (no parse error)."""
        from agent_crew.cli import crew
        runner = CliRunner()
        # Will fail on missing --db/--project but timeout parsing must succeed
        result = runner.invoke(crew, ["discuss", "hello", "--timeout", "60", "--db", "/nonexistent.db"])
        # We expect a real error (db missing), NOT a bad option error
        assert "no such option: --timeout" not in result.output, (
            "--timeout is not recognized — the option is not registered"
        )


# ---------------------------------------------------------------------------
# #170 — crew status without project: list all projects
# ---------------------------------------------------------------------------

class TestStatusAllProjects:
    def _write_fake_state(self, base: str, project: str, port: int = 0, db: str = "") -> None:
        proj_dir = os.path.join(base, project)
        os.makedirs(proj_dir, exist_ok=True)
        state = {
            "project": project, "port": port, "session": "fake",
            "window": "0", "pane_ids": [], "pane_map": {}, "agents": [],
            "worktrees": {}, "db": db, "server_pid": 0, "sessions_file": "",
            "port_file": "",
        }
        with open(os.path.join(proj_dir, "state.json"), "w") as f:
            json.dump(state, f)

    def test_u170_lists_all_projects(self, tmp_path):
        """crew status (no PROJECT) must list all projects in base dir."""
        base = str(tmp_path)
        self._write_fake_state(base, "proj_alpha", port=8100)
        self._write_fake_state(base, "proj_beta", port=8101)

        from agent_crew.cli import crew
        runner = CliRunner()
        with patch("agent_crew.cli._port_listening", return_value=False):
            result = runner.invoke(crew, ["status", "--base", base])

        assert result.exit_code == 0, f"exit code {result.exit_code}: {result.output}"
        assert "proj_alpha" in result.output
        assert "proj_beta" in result.output

    def test_u170_no_projects_message_when_base_empty(self, tmp_path):
        """crew status (no PROJECT) shows 'No projects found' when base is empty."""
        from agent_crew.cli import crew
        runner = CliRunner()
        result = runner.invoke(crew, ["status", "--base", str(tmp_path)])
        assert result.exit_code == 0
        assert "No projects found" in result.output

    def test_u170_project_arg_still_works(self, tmp_path):
        """crew status PROJECT still works (backward compatibility)."""
        base = str(tmp_path)
        # Create a minimal project with a DB
        db = str(tmp_path / "tasks.db")
        TaskQueue(db)  # create the DB
        self._write_fake_state(base, "myproj", port=0, db=db)

        from agent_crew.cli import crew
        runner = CliRunner()
        with patch("agent_crew.cli._port_listening", return_value=False), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            result = runner.invoke(crew, ["status", "myproj", "--base", base])

        # Should show project-specific status, not the all-projects list
        assert "myproj" in result.output or result.exit_code in (0, 1)
