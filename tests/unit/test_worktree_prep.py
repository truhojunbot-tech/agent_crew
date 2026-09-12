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


def _resolving_run(cmds, sha="a" * 40, fail_refs=()):
    """A fake `git` that resolves refs, so prep reaches its checkout."""
    def fake_run(cmd, **_kw):
        cmds.append(cmd)
        if "rev-parse" in cmd:
            ref = cmd[-1]
            if any(bad in ref for bad in fail_refs):
                return MagicMock(returncode=1, stdout="", stderr="unknown revision")
            return MagicMock(returncode=0, stdout=sha + "\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")
    return fake_run


def test_prepare_reviewer_pins_the_pr_branch_to_an_exact_commit():
    """Reviewer: stash → fetch → rev-parse origin/<task_branch> → detach at it.

    ⛔Was `checkout -B review/<id[:8]>` until #286. Two things changed and both
      are deliberate: the ref is resolved to an object BEFORE checkout, because
      `reviewed_sha` is read straight after and a name can move between the two
      (#286); and no branch is created, because reviewer and tester never commit
      and a branch is one more shared `refs/heads/*` write (#280).
    """
    cmds = []
    with patch("agent_crew.server.subprocess.run",
               side_effect=_resolving_run(cmds)):
        _prepare_worktree_for_task(
            "/wt/codex", "aabb11221122", "agent/feat-xyz", "reviewer"
        )

    git = _git_calls(cmds)
    assert any("rev-parse" in c and "origin/agent/feat-xyz" in " ".join(c) for c in git)
    checkout = [c for c in git if "checkout" in c]
    assert checkout and "--detach" in checkout[0], checkout
    assert checkout[0][-1] == "a" * 40, checkout
    assert not [c for c in checkout if "-B" in c], "prep created a branch ref"


def test_prepare_tester_pins_the_pr_branch_the_same_way():
    cmds = []
    with patch("agent_crew.server.subprocess.run",
               side_effect=_resolving_run(cmds)):
        _prepare_worktree_for_task(
            "/wt/gemini", "ccdd33441122", "agent/feat-xyz", "tester"
        )

    git = _git_calls(cmds)
    checkout = [c for c in git if "checkout" in c]
    assert checkout and checkout[0][-1] == "a" * 40 and "--detach" in checkout[0]
    assert not [c for c in checkout if "-B" in c]


def test_prepare_reviewer_falls_back_to_main_if_branch_absent():
    """If origin/<task_branch> does not resolve, fall back to origin/main.

    ⛔The fallback moved from checkout-time to RESOLVE-time (#286): there is now
      one checkout, at whichever ref produced a commit. A second checkout would
      mean the worktree had already been moved once."""
    cmds = []
    with patch("agent_crew.server.subprocess.run",
               side_effect=_resolving_run(cmds, fail_refs=("gone-branch",))):
        _prepare_worktree_for_task(
            "/wt/codex", "review-eeff5566", "agent/gone-branch", "reviewer"
        )

    git = _git_calls(cmds)
    probed = [" ".join(c) for c in git if "rev-parse" in c]
    assert any("origin/agent/gone-branch" in p for p in probed), probed
    assert any("origin/main" in p for p in probed), probed
    checkout = [c for c in git if "checkout" in c]
    assert len(checkout) == 1, f"expected one checkout, got {checkout}"


def test_prepare_worktree_failure_does_not_raise():
    """subprocess failures must be swallowed — dispatch must continue."""
    with patch("agent_crew.server.subprocess.run", side_effect=OSError("git not found")):
        # Should not raise
        try:
            _prepare_worktree_for_task("/wt/claude", "t-1", "branch", "implementer")
        except Exception as e:
            raise AssertionError(f"should not raise: {e}") from e


# ---------------------------------------------------------------------------
# Every git subprocess.run call here must bound its own wall-clock time.
#
# Observed live on alpha_engine (2026-08-27): under host-level memory/swap
# pressure, a `git fetch` in one worktree got stuck in uninterruptible disk
# I/O (D state — not even killable by signal). That call already had
# timeout=60, but stash/checkout did not, and subprocess.run() is a plain
# blocking call made directly inside this async dispatch path — no timeout
# means no bound on how long it can freeze the *entire* event loop,
# including unrelated HTTP requests like /health. A timeout can't rescue a
# process already wedged in D state, but it does bound every OTHER call
# here so a slow-but-not-wedged git op fails its one task instead of being
# able to hang indefinitely.
# ---------------------------------------------------------------------------

def _calls_with_kwargs(monkeypatch):
    """Patch subprocess.run and return a list that accumulates (cmd, kwargs)."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr("agent_crew.server.subprocess.run", fake_run)
    return calls


def test_every_git_call_has_a_timeout_implementer_path(monkeypatch):
    calls = _calls_with_kwargs(monkeypatch)

    _prepare_worktree_for_task("/wt/claude", "task-abc123", "agent/feat-xyz", "implementer")

    git_calls = [(cmd, kw) for cmd, kw in calls if cmd[:2] == ["git", "-C"]]
    assert len(git_calls) >= 3, f"expected stash+fetch+checkout, got {git_calls}"
    for cmd, kw in git_calls:
        assert kw.get("timeout") is not None, f"no timeout on: {cmd}"


def test_every_git_call_has_a_timeout_reviewer_path_including_fallback(monkeypatch):
    calls = []
    checkout_count = [0]

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        if "rev-parse" in cmd:
            if "gone-branch" in cmd[-1]:
                return MagicMock(returncode=1, stdout="", stderr="unknown revision")
            return MagicMock(returncode=0, stdout="a" * 40, stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("agent_crew.server.subprocess.run", fake_run)

    _prepare_worktree_for_task("/wt/codex", "review-eeff5566", "agent/gone-branch", "reviewer")

    git_calls = [(cmd, kw) for cmd, kw in calls if cmd[:2] == ["git", "-C"]]
    # ⛔The point of this test is the timeout on EVERY call, and #286 added the
    #   rev-parse probes — a new unbounded git call in the dispatch path is
    #   exactly what this guards against (a stuck git freezes the event loop).
    probes = [c for c, _ in git_calls if "rev-parse" in c and "--verify" in c]
    assert len(probes) == 2, probes
    for cmd, kw in git_calls:
        assert kw.get("timeout") is not None, f"no timeout on: {cmd}"


def test_stuck_git_process_fails_only_its_own_task_not_the_dispatcher():
    """A git call that exceeds its timeout raises TimeoutExpired — the outer
    wrapper must swallow it exactly like any other subprocess failure
    (OSError case above), not let it propagate and take dispatch down with
    it."""
    import subprocess as subprocess_module

    def fake_run(cmd, **kw):
        if "stash" in cmd:
            raise subprocess_module.TimeoutExpired(cmd=cmd, timeout=kw.get("timeout", 30))
        return MagicMock(returncode=0, stderr="")

    with patch("agent_crew.server.subprocess.run", side_effect=fake_run):
        try:
            _prepare_worktree_for_task("/wt/claude", "t-2", "branch", "implementer")
        except Exception as e:
            raise AssertionError(f"should not raise: {e}") from e
