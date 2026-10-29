"""#908 — the rebase_onto floor is the dispatch-time target, not the result-time tip.

impl-825cp rebased onto main 0337d11 and pushed a commit descending from it.
main then moved to 06bb835 before the result POST; the gate re-fetched main
and demanded descent from the new tip, discarding a valid artifact.
"""
from __future__ import annotations

import subprocess

import pytest

from agent_crew.protocol import TaskRequest, TaskResult


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          text=True, capture_output=True).stdout.strip()


def _commit(clone, name, text):
    (clone / name).write_text(text)
    _git(clone, "add", name)
    _git(clone, "commit", "-m", name)
    return _git(clone, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    origin = tmp_path / "origin.git"
    clone = tmp_path / "clone"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "clone", str(origin), str(clone))
    _git(clone, "config", "user.email", "crew@example.test")
    _git(clone, "config", "user.name", "Crew Test")
    root = _commit(clone, "base.txt", "base\n")
    _git(clone, "branch", "-M", "main")
    _git(clone, "push", "-u", "origin", "main")
    return clone, root


def _advance_main(clone, name):
    """Merge someone else's PR into origin/main after dispatch."""
    _git(clone, "switch", "main")
    tip = _commit(clone, name, "other PR\n")
    _git(clone, "push", "origin", "main")
    return tip


def _verify(clone, floor, commit, branch="feat/work"):
    from agent_crew.pipeline import verify_implement_artifact

    task = TaskRequest("impl-908", "implement", "x", branch=branch,
                       context={"worktree_base_sha": floor, "rebase_onto": "main"})
    result = TaskResult("impl-908", "completed", "done", branch=branch, commit=commit)
    return verify_implement_artifact(task, result, repo_cwd=str(clone))


def test_rebase_accepted_when_target_advances_after_dispatch(repo):
    clone, dispatch_tip = repo
    _git(clone, "switch", "-c", "feat/work")
    commit = _commit(clone, "feature.txt", "rebased\n")
    _git(clone, "push", "-u", "origin", "feat/work")
    new_tip = _advance_main(clone, "other.txt")

    assert subprocess.run(["git", "merge-base", "--is-ancestor", new_tip, commit],
                          cwd=clone).returncode == 1
    ok, detail = _verify(clone, dispatch_tip, commit)
    assert ok is True, detail


def test_commit_not_descending_from_dispatch_target_still_rejected(repo):
    clone, root = repo
    dispatch_tip = _commit(clone, "second.txt", "main moved before dispatch\n")
    _git(clone, "push", "origin", "main")
    # Built on the pre-dispatch root: never rebased onto the dispatch target.
    _git(clone, "switch", "-c", "feat/work", root)
    commit = _commit(clone, "feature.txt", "stale\n")
    _git(clone, "push", "-u", "origin", "feat/work")
    _advance_main(clone, "other.txt")

    ok, detail = _verify(clone, dispatch_tip, commit)
    assert ok is False
    assert "does not descend from the rebase_onto target" in detail


@pytest.mark.parametrize("which", ["dispatch", "current"])
def test_commit_equal_to_target_still_rejected(repo, which):
    clone, dispatch_tip = repo
    new_tip = _advance_main(clone, "other.txt")
    commit = dispatch_tip if which == "dispatch" else new_tip
    _git(clone, "push", "origin", f"{commit}:refs/heads/feat/work")

    ok, detail = _verify(clone, dispatch_tip, commit)
    assert ok is False
    assert "no new artifact" in detail


def test_floor_off_target_history_falls_back_to_current_tip(repo):
    """A worktree_base_sha that is the branch head (not on main) is no floor."""
    clone = repo[0]
    _git(clone, "switch", "-c", "feat/old")
    old_head = _commit(clone, "feature.txt", "pre-rebase head\n")
    new_tip = _advance_main(clone, "other.txt")
    _git(clone, "switch", "-c", "feat/work")
    commit = _commit(clone, "feature.txt", "rebased onto current tip\n")
    _git(clone, "push", "-u", "origin", "feat/work")
    assert subprocess.run(["git", "merge-base", "--is-ancestor", new_tip, commit],
                          cwd=clone).returncode == 0

    ok, detail = _verify(clone, old_head, commit)
    assert ok is True, detail


# ── declared rebase contract (artifact_kind=rebase, review of PR #909) ──────

def _verify_declared(clone, source, commit, floor=None, branch="feat/work"):
    from agent_crew.pipeline import verify_rebase_artifact

    context = {"artifact_kind": "rebase", "rebase_onto": "main",
               "rebase_source_sha": source, "worktree_base_sha": source}
    if floor is not None:
        context["rebase_onto_sha"] = floor
    task = TaskRequest("rebase-908", "implement", "x", branch=branch, context=context)
    result = TaskResult("rebase-908", "completed", "done", branch=branch, commit=commit)
    return verify_rebase_artifact(task, result, repo_cwd=str(clone))


@pytest.fixture
def diverged(repo):
    """feat/work forked at root; main moved to the dispatch-time tip since."""
    clone, root = repo
    _git(clone, "switch", "-c", "feat/old", root)
    source = _commit(clone, "feature.txt", "feature\n")
    _git(clone, "push", "-u", "origin", "feat/old")
    _git(clone, "switch", "main")
    dispatch_tip = _commit(clone, "second.txt", "main before dispatch\n")
    _git(clone, "push", "origin", "main")
    return clone, root, source, dispatch_tip


def _rebase_onto(clone, source, onto):
    _git(clone, "switch", "-C", "feat/work", onto)
    _git(clone, "cherry-pick", source)
    # A distinct message: a same-second cherry-pick onto the source's own
    # parent would otherwise reproduce the source SHA exactly.
    _git(clone, "commit", "--amend", "-m", f"rebased onto {onto[:12]}")
    commit = _git(clone, "rev-parse", "HEAD")
    _git(clone, "push", "-f", "origin", "feat/work")
    return commit


def test_declared_rebase_accepted_when_target_advances_after_dispatch(diverged):
    clone, _root, source, dispatch_tip = diverged
    commit = _rebase_onto(clone, source, dispatch_tip)
    new_tip = _advance_main(clone, "other.txt")
    assert subprocess.run(["git", "merge-base", "--is-ancestor", new_tip, commit],
                          cwd=clone).returncode == 1

    ok, detail = _verify_declared(clone, source, commit, floor=dispatch_tip)
    assert ok is True, detail


def test_declared_rebase_onto_point_between_floor_and_tip_accepted(diverged):
    clone, _root, source, dispatch_tip = diverged
    mid = _advance_main(clone, "mid.txt")
    commit = _rebase_onto(clone, source, mid)
    _advance_main(clone, "other.txt")

    ok, detail = _verify_declared(clone, source, commit, floor=dispatch_tip)
    assert ok is True, detail


def test_declared_rebase_below_dispatch_floor_rejected(diverged):
    clone, root, source, dispatch_tip = diverged
    commit = _rebase_onto(clone, source, root)  # not rebased at all
    _advance_main(clone, "other.txt")

    ok, detail = _verify_declared(clone, source, commit, floor=dispatch_tip)
    assert ok is False
    assert "does not descend from origin/main" in detail


def test_declared_rebase_changed_content_still_rejected(diverged):
    clone, _root, source, dispatch_tip = diverged
    _git(clone, "switch", "-C", "feat/work", dispatch_tip)
    commit = _commit(clone, "feature.txt", "something else\n")
    _git(clone, "push", "-f", "origin", "feat/work")
    _advance_main(clone, "other.txt")

    ok, detail = _verify_declared(clone, source, commit, floor=dispatch_tip)
    assert ok is False
    assert "not patch-equivalent" in detail


@pytest.mark.parametrize("which", ["dispatch", "current"])
def test_declared_rebase_commit_on_target_is_no_artifact(diverged, which):
    clone, _root, source, dispatch_tip = diverged
    new_tip = _advance_main(clone, "other.txt")
    commit = dispatch_tip if which == "dispatch" else new_tip
    _git(clone, "push", "-f", "origin", f"{commit}:refs/heads/feat/work")

    ok, detail = _verify_declared(clone, source, commit, floor=dispatch_tip)
    assert ok is False
    assert "no artifact" in detail


def test_declared_floor_off_target_history_falls_back_to_current_tip(diverged):
    """An unpinned floor (source only) keeps the strict current-tip rule."""
    clone, _root, source, dispatch_tip = diverged
    commit = _rebase_onto(clone, source, dispatch_tip)
    _advance_main(clone, "other.txt")

    ok, detail = _verify_declared(clone, source, commit, floor=source)
    assert ok is False
    assert "does not descend from origin/main" in detail
    new_commit = _rebase_onto(clone, source, _git(clone, "rev-parse", "origin/main"))
    ok, detail = _verify_declared(clone, source, new_commit)
    assert ok is True, detail
