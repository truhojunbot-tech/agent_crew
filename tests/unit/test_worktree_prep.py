"""Unit tests for worktree sync/branch-prep before task dispatch (#140, #141).

- _prepare_worktree_for_task: stash → fetch → checkout per role
- _load_worktree_map: reads {role: path} from state.json
"""
import json
from unittest.mock import call, patch, MagicMock

from agent_crew.server import _load_worktree_map, _prepare_worktree_for_task


# ---------------------------------------------------------------------------
# _load_worktree_map
# ---------------------------------------------------------------------------


def test_load_worktree_map_derives_roles(tmp_path):
    """state.json with agent-name keys → {role: path} mapping."""
    state = {
        "worktrees": {
            "claude": "/wt/claude",
            "codex": "/wt/codex",
            "gemini": "/wt/gemini",
        }
    }
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state))

    wm = _load_worktree_map(str(state_file))
    assert wm["implementer"] == "/wt/claude"
    assert wm["reviewer"] == "/wt/codex"
    assert wm["tester"] == "/wt/gemini"


def test_load_worktree_map_missing_file():
    assert _load_worktree_map("/nonexistent/state.json") == {}


def test_load_worktree_map_none():
    assert _load_worktree_map(None) == {}


def test_load_worktree_map_empty_worktrees(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"worktrees": {}}))
    assert _load_worktree_map(str(state_file)) == {}


# ---------------------------------------------------------------------------
# _prepare_worktree_for_task — implementer (#140)
# ---------------------------------------------------------------------------


def _git_calls(cmds_list):
    """Filter raw cmd lists (as collected by a side_effect func) to git -C calls."""
    return [c for c in cmds_list if isinstance(c, list) and c[:2] == ["git", "-C"]]


def test_prepare_implementer_checks_out_task_branch():
    """Implementer: stash → fetch → checkout -B <task_branch> origin/main."""
    cmds = []

    def fake_run(cmd, **_kw):
        cmds.append(cmd)
        return MagicMock(returncode=0, stderr="")

    with patch("agent_crew.server.subprocess.run", side_effect=fake_run):
        _prepare_worktree_for_task(
            "/wt/claude", "task-abc123", "agent/feat-xyz", "implementer"
        )

    git = _git_calls(cmds)
    # stash
    assert any("stash" in " ".join(c) for c in git)
    # fetch
    assert any("fetch" in " ".join(c) for c in git)
    # checkout -B agent/feat-xyz origin/main
    checkout = [c for c in git if "checkout" in c]
    assert checkout, "no checkout call"
    assert any("agent/feat-xyz" in " ".join(c) for c in checkout)
    assert any("origin/main" in " ".join(c) for c in checkout)


def test_prepare_implementer_derives_branch_from_task_id_when_empty():
    """When task.branch is empty, implementer branch is derived from task_id (first 12 chars)."""
    cmds = []

    def fake_run(cmd, **_kw):
        cmds.append(cmd)
        return MagicMock(returncode=0, stderr="")

    # task_id without hyphen prefix so first 12 chars are predictable
    with patch("agent_crew.server.subprocess.run", side_effect=fake_run):
        _prepare_worktree_for_task("/wt/claude", "deadbeef123456", "", "implementer")

    git = _git_calls(cmds)
    checkout = [c for c in git if "checkout" in c]
    assert any("agent/deadbeef1234" in " ".join(c) for c in checkout)


# ---------------------------------------------------------------------------
# _prepare_worktree_for_task — reviewer/tester (#141)
# ---------------------------------------------------------------------------


def test_prepare_reviewer_checks_out_pr_branch():
    """Reviewer: stash → fetch → checkout -B review/<id[:8]> origin/<task_branch>."""
    cmds = []

    def fake_run(cmd, **_kw):
        cmds.append(cmd)
        return MagicMock(returncode=0, stderr="")

    # Use task_id without hyphen prefix so first 8 chars are predictable
    with patch("agent_crew.server.subprocess.run", side_effect=fake_run):
        _prepare_worktree_for_task(
            "/wt/codex", "aabb11221122", "agent/feat-xyz", "reviewer"
        )

    git = _git_calls(cmds)
    checkout = [c for c in git if "checkout" in c]
    assert any("review/aabb1122" in " ".join(c) for c in checkout), checkout
    assert any("origin/agent/feat-xyz" in " ".join(c) for c in checkout), checkout


def test_prepare_tester_checks_out_pr_branch():
    """Tester uses 'test/<id[:8]>' prefix."""
    cmds = []

    def fake_run(cmd, **_kw):
        cmds.append(cmd)
        return MagicMock(returncode=0, stderr="")

    with patch("agent_crew.server.subprocess.run", side_effect=fake_run):
        _prepare_worktree_for_task(
            "/wt/gemini", "ccdd33441122", "agent/feat-xyz", "tester"
        )

    git = _git_calls(cmds)
    checkout = [c for c in git if "checkout" in c]
    assert any("test/ccdd3344" in " ".join(c) for c in checkout), checkout


def test_prepare_reviewer_falls_back_to_main_if_branch_absent():
    """If origin/<task_branch> doesn't exist, fall back to origin/main."""
    cmds = []
    checkout_count = [0]

    def fake_run(cmd, **_kw):
        cmds.append(cmd)
        if "checkout" in cmd:
            checkout_count[0] += 1
            if checkout_count[0] == 1:
                # First checkout: target PR branch → fail (branch gone/absent)
                return MagicMock(returncode=1, stderr="pathspec not found")
        return MagicMock(returncode=0, stderr="")

    with patch("agent_crew.server.subprocess.run", side_effect=fake_run):
        _prepare_worktree_for_task(
            "/wt/codex", "review-eeff5566", "agent/gone-branch", "reviewer"
        )

    git = _git_calls(cmds)
    checkout = [c for c in git if "checkout" in c]
    # Second checkout should target origin/main as fallback
    assert len(checkout) == 2, f"expected 2 checkouts, got {len(checkout)}: {checkout}"
    assert any("origin/main" in " ".join(c) for c in checkout), checkout


def test_prepare_worktree_failure_does_not_raise():
    """subprocess failures must be swallowed — dispatch must continue."""
    with patch("agent_crew.server.subprocess.run", side_effect=OSError("git not found")):
        # Should not raise
        try:
            _prepare_worktree_for_task("/wt/claude", "t-1", "branch", "implementer")
        except Exception as e:
            raise AssertionError(f"should not raise: {e}") from e
