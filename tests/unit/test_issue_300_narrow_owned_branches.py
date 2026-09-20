"""#300 — only generated agent branches may be force-reset."""

import subprocess

import pytest

from agent_crew.server import _agent_crew_owns_branch, _prepare_worktree_for_task


def _git(*args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          timeout=60, check=check)


def _sha(repo, ref):
    return _git("rev-parse", ref, cwd=repo).stdout.strip()


@pytest.fixture
def shared_clone(tmp_path):
    origin = tmp_path / "origin.git"
    _git("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    clone = tmp_path / "clone"
    _git("clone", str(origin), str(clone), cwd=tmp_path)
    _git("config", "user.email", "t@example.com", cwd=clone)
    _git("config", "user.name", "t", cwd=clone)
    (clone / "a.txt").write_text("main\n")
    _git("add", "a.txt", cwd=clone)
    _git("commit", "-m", "main", cwd=clone)
    _git("push", "-u", "origin", "main", cwd=clone)
    return clone


def _detached_worktree(clone, path):
    _git("worktree", "add", "--detach", str(path), "origin/main", cwd=clone)
    return path


def test_developer_agent_path_ahead_of_origin_survives_two_preparations(shared_clone, tmp_path):
    """Real shared refs: alternating implementer worktrees must not move it."""
    clone = shared_clone
    branch = "agent/claude-cli/1-x"
    _git("checkout", "-b", branch, cwd=clone)
    (clone / "a.txt").write_text("developer-only\n")
    _git("commit", "-am", "unpushed developer work", cwd=clone)
    before = _sha(clone, branch)
    _git("checkout", "main", cwd=clone)
    first = _detached_worktree(clone, tmp_path / "first")
    second = _detached_worktree(clone, tmp_path / "second")

    _prepare_worktree_for_task(str(first), "0123456789ab", branch, "implementer")
    _prepare_worktree_for_task(str(second), "0123456789ab", branch, "implementer")

    assert _sha(clone, branch) == before


def test_broad_agent_prefix_mutant_moves_the_developer_ref(shared_clone, tmp_path):
    """Mutation check: the pre-#300 ``agent/`` rule loses scenario (a)."""
    clone = shared_clone
    branch = "agent/claude-cli/1-x"
    _git("checkout", "-b", branch, cwd=clone)
    (clone / "a.txt").write_text("developer-only\n")
    _git("commit", "-am", "unpushed developer work", cwd=clone)
    before = _sha(clone, branch)
    _git("checkout", "main", cwd=clone)
    worktree = _detached_worktree(clone, tmp_path / "mutant")

    # This is exactly the old broad-prefix implementer path.
    _git("checkout", "-B", branch, "origin/main", cwd=worktree)

    assert _sha(clone, branch) != before


def test_owned_branch_with_unpushed_commit_is_preserved_on_retry(shared_clone, tmp_path, caplog):
    clone = shared_clone
    branch = "agent/0123456789ab"
    _git("checkout", "-b", branch, cwd=clone)
    (clone / "a.txt").write_text("agent-only\n")
    _git("commit", "-am", "unpushed agent work", cwd=clone)
    before = _sha(clone, branch)
    _git("checkout", "main", cwd=clone)
    worktree = _detached_worktree(clone, tmp_path / "retry")

    _prepare_worktree_for_task(str(worktree), "0123456789ab", branch, "implementer")

    assert _sha(clone, branch) == before
    assert _git("symbolic-ref", "-q", "HEAD", cwd=worktree, check=False).returncode != 0
    assert before in caplog.text


@pytest.mark.parametrize(("branch", "owned"), [
    ("agent/claude-cli/x", False),
    ("agent/0123456789ab", True),
    ("agent/project/claude", True),
    ("review/x", True),
    ("test/x", True),
])
def test_owned_branch_shapes_are_deliberately_narrow(branch, owned):
    assert _agent_crew_owns_branch(branch) is owned
