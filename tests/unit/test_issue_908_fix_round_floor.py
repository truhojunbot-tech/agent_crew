"""#908 follow-up — a fix round's rebase floor is the target SHA pinned at dispatch.

PR #909 floors the rebase_onto descent check at ``worktree_base_sha`` only when
that SHA is on the target's history. A fix round's base is the PR branch head,
which is not on main, so the floor fell back to the live tip and any main
commit landing during the round rejected a correct rebase (athena440/441 r2).
"""
from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from agent_crew import server as sv
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue

REJECT = "reported commit does not descend from the rebase_onto target"
NO_NEW = "reported commit is the rebase_onto target (no new artifact)"


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          text=True, capture_output=True).stdout.strip()


def _commit(clone, name, text):
    (clone / name).write_text(text)
    _git(clone, "add", name)
    _git(clone, "commit", "-m", name)
    return _git(clone, "rev-parse", "HEAD")


def _is_ancestor(clone, older, newer):
    return subprocess.run(["git", "merge-base", "--is-ancestor", older, newer],
                          cwd=clone).returncode == 0


@pytest.fixture
def repo(tmp_path):
    """main M0 → M1, plus a PR branch head P that is not on main."""
    origin = tmp_path / "origin.git"
    clone = tmp_path / "clone"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    _git(tmp_path, "clone", str(origin), str(clone))
    _git(clone, "config", "user.email", "crew@example.test")
    _git(clone, "config", "user.name", "Crew Test")
    _git(clone, "switch", "-c", "main")
    m0 = _commit(clone, "base.txt", "base\n")
    _git(clone, "push", "-u", "origin", "main")
    _git(clone, "switch", "-c", "agent/pr", m0)
    pr_head = _commit(clone, "pr.txt", "pr round 1\n")
    _git(clone, "push", "-u", "origin", "agent/pr")
    _git(clone, "switch", "main")
    m1 = _commit(clone, "m1.txt", "main moved before dispatch\n")
    _git(clone, "push", "origin", "main")
    return clone, m0, m1, pr_head


def _advance_main(clone, name):
    _git(clone, "switch", "main")
    tip = _commit(clone, name, "records-only commit\n")
    _git(clone, "push", "origin", "main")
    return tip


def _rebased_fix(clone, onto, name="fix.txt"):
    """The worker's fix round: PR branch rebased onto ``onto`` plus a fix."""
    _git(clone, "switch", "-C", "work", onto)
    _commit(clone, "pr.txt", "pr round 1\n")
    commit = _commit(clone, name, "fix\n")
    _git(clone, "push", "-f", "origin", "HEAD:agent/pr")
    return commit


def _verify(clone, commit, *, base, floor="__unset__"):
    from agent_crew.pipeline import verify_implement_artifact

    context = {"worktree_base_sha": base, "rebase_onto": "main"}
    if floor != "__unset__":
        context["rebase_onto_floor_sha"] = floor
    task = TaskRequest("fix-impl-908", "implement", "x", branch="agent/pr", context=context)
    result = TaskResult("fix-impl-908", "completed", "done", branch="agent/pr", commit=commit)
    return verify_implement_artifact(task, result, repo_cwd=str(clone))


def test_fix_round_accepted_when_main_advances_after_dispatch(repo):
    clone, _m0, m1, pr_head = repo
    assert not _is_ancestor(clone, pr_head, m1)
    commit = _rebased_fix(clone, m1)
    m2 = _advance_main(clone, "m2.txt")
    assert not _is_ancestor(clone, m2, commit)

    ok, detail = _verify(clone, commit, base=pr_head, floor=m1)
    assert ok is True, detail


def test_fix_round_not_descending_from_floor_still_rejected(repo):
    clone, m0, m1, pr_head = repo
    commit = _rebased_fix(clone, m0)  # never rebased onto the dispatch target M1
    _advance_main(clone, "m2.txt")

    assert _verify(clone, commit, base=pr_head, floor=m1) == (False, REJECT)


def test_commit_equal_to_target_tip_or_floor_rejected(repo):
    clone, _m0, m1, pr_head = repo
    assert _verify(clone, m1, base=pr_head, floor=m1) == (False, NO_NEW)
    m2 = _advance_main(clone, "m2.txt")
    _git(clone, "push", "-f", "origin", f"{m1}:refs/heads/agent/pr")
    assert _verify(clone, m1, base=pr_head, floor=m1) == (False, NO_NEW)
    _git(clone, "push", "-f", "origin", f"{m2}:refs/heads/agent/pr")
    assert _verify(clone, m2, base=pr_head, floor=m1) == (False, NO_NEW)


def test_floor_not_on_target_history_is_ignored(repo):
    clone, _m0, m1, pr_head = repo
    commit = _rebased_fix(clone, m1)
    _advance_main(clone, "m2.txt")
    # A recorded floor that main no longer contains is no floor: live-tip rule.
    assert _verify(clone, commit, base=pr_head, floor=pr_head) == (False, REJECT)


@pytest.mark.parametrize("floor", ["__unset__", None])
def test_missing_floor_keeps_909_behaviour(repo, floor):
    clone, _m0, m1, pr_head = repo
    commit = _rebased_fix(clone, m1)
    # #909 unchanged: no floor + off-main base → live tip, which is M1 here.
    assert _verify(clone, commit, base=pr_head, floor=floor)[0] is True
    _advance_main(clone, "m2.txt")
    # Off-main base → live tip M2 → rejected (the #908 loss this PR fixes).
    assert _verify(clone, commit, base=pr_head, floor=floor) == (False, REJECT)
    # On-main base → the #909 base floor still accepts.
    assert _verify(clone, commit, base=m1, floor=floor)[0] is True


def _app(tmp_path, monkeypatch, worker):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE__DEMO", "off")
    monkeypatch.setattr(sv, "_resolve_tmux_pane_target", lambda pane: pane)
    monkeypatch.setattr(sv, "_pane_alive_for_push", lambda pane: True)
    monkeypatch.setattr(sv, "_ensure_role_protocol", lambda *a, **kw: True)
    db = str(tmp_path / "tasks.db")
    app = sv.create_app(
        db, worktree_map={"implementer": str(worker)}, pane_map={"implementer": "%91"},
        push_fn=lambda *args: None,
        identity_required=False, watchdog_disabled=True, anomaly_disabled=True,
    )
    return app, TaskQueue(db)


@pytest.mark.parametrize("target, expected", [("main", "m1"), ("no-such-branch", None)])
def test_dispatch_records_rebase_onto_floor(repo, tmp_path, monkeypatch, target, expected):
    clone, _m0, m1, pr_head = repo
    _git(clone, "switch", "--detach", pr_head)
    app, queue = _app(tmp_path, monkeypatch, clone)
    queue.enqueue(TaskRequest(
        "fix-impl-908-d", "implement", "fix", branch="agent/pr", project="demo",
        context={"worktree_base_sha": pr_head, "rebase_onto": target},
    ))
    task = queue.get_task("fix-impl-908-d")

    with TestClient(app):
        assert app.state.record_prepared_base(task, "implementer", pr_head, "test")
    context = queue.get_task_context(task.task_id)
    assert context["worktree_base_sha"] == pr_head
    assert context.get("rebase_onto_floor_sha") == (m1 if expected else None)
    assert task.context.get("rebase_onto_floor_sha") == (m1 if expected else None)


def test_dispatch_without_rebase_onto_records_no_floor(repo, tmp_path, monkeypatch):
    clone, _m0, _m1, pr_head = repo
    app, queue = _app(tmp_path, monkeypatch, clone)
    queue.enqueue(TaskRequest("impl-908-plain", "implement", "x", branch="agent/pr",
                              project="demo", context={}))
    task = queue.get_task("impl-908-plain")

    with TestClient(app):
        assert app.state.record_prepared_base(task, "implementer", pr_head, "test")
    assert "rebase_onto_floor_sha" not in queue.get_task_context(task.task_id)
