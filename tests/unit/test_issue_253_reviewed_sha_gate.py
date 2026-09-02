"""#253 — a finding must be about the current code, not a state already fixed.

Measured on PR #251: of seven review rounds, **five** produced fix tasks for
work that already existed. Each cost a reviewer invocation, an implementer
invocation and a review round, and each could only conclude "does not
reproduce".

The round cap cannot catch this. `AGENT_CREW_REVIEW_FIX_MAX_ROUNDS` counts
rounds within one lineage, and every new review opens a fresh lineage at round
1 — two of those duplicates arrived labelled `3/3`, a spent budget, while the
next review started another. Only comparing the reviewed commit with the
current head closes it.
"""

import uuid

import pytest

from agent_crew.pipeline import auto_enqueue_fix, review_is_current
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue

BRANCH = "fix/250-terminal-pr-gate"
PR = 251
OLD = "33245e4a1b2c3d4e5f60718293a4b5c6d7e8f900"
NEW = "c95519e96588f9a7da0e6f2e004a2ace12987449"
FINDING = "HIGH the announcement lease has no fencing token"


def _head(sha):
    return lambda pr: sha


@pytest.fixture
def q(tmp_db):
    return TaskQueue(tmp_db)


def _review(q, *, reviewed_sha=OLD, pr_number=PR, verdict="request_changes",
            findings=(FINDING,)):
    rid = f"review-{uuid.uuid4().hex[:8]}"
    ctx = {"prev_task_id": "impl-1", "pr_number": pr_number}
    if reviewed_sha:
        ctx["reviewed_sha"] = reviewed_sha
    q.enqueue(TaskRequest(task_id=rid, task_type="review", description="review",
                          branch=BRANCH, context=ctx))
    q.submit_result(rid, TaskResult(task_id=rid, status="completed",
                                    summary="request_changes", verdict=verdict,
                                    findings=list(findings), pr_number=pr_number))
    return rid


# ── 1. the comparison ─────────────────────────────────────────────────


def test_a_review_of_the_current_head_is_current(q):
    assert review_is_current({"reviewed_sha": NEW}, PR, head_sha_fn=_head(NEW))[0] is True


def test_a_review_of_an_older_commit_is_not(q):
    current, why = review_is_current({"reviewed_sha": OLD}, PR, head_sha_fn=_head(NEW))

    assert current is False
    assert OLD[:9] in why and NEW[:9] in why, "the reason must name both commits"


def test_an_unknown_head_defers_rather_than_guesses():
    """⛔Same rule as the terminal-PR gate: a skipped cascade is recoverable, a
    fix task written against a state that no longer exists is not.

    The REASON is pinned too, not only the decision. Falling through to the
    comparison would also return "not current", but it would report "the head
    is now " with an empty commit — an operator reading that would think the
    review was superseded when in fact we never learned anything.
    """
    current, why = review_is_current({"reviewed_sha": OLD}, PR, head_sha_fn=_head(""))
    assert current is False
    assert "unknown" in why, f"misleading reason for an unreadable head: {why!r}"

    def boom(pr):
        raise RuntimeError("gh is down")

    current, why = review_is_current({"reviewed_sha": OLD}, PR, head_sha_fn=boom)
    assert current is False and "unknown" in why


def test_a_review_with_no_recorded_sha_still_cascades():
    """⛔Backward compatibility is load-bearing here. Tasks predating this
    change, and producers that never go through worktree prep, have no
    `reviewed_sha` — refusing to act on all of them would break the pipeline
    to fix a subset of it."""
    assert review_is_current({}, PR, head_sha_fn=_head(NEW))[0] is True
    assert review_is_current({"reviewed_sha": OLD}, None, head_sha_fn=_head(NEW))[0] is True


# ── 2. the cascade ────────────────────────────────────────────────────


def test_a_stale_review_creates_no_fix_task(q):
    """★The regression: the finding was fixed between review and dispatch."""
    review_id = _review(q, reviewed_sha=OLD)

    assert auto_enqueue_fix(q, review_id, head_sha_fn=_head(NEW)) is None
    assert not [t for t in q.list_tasks() if t.task_type == "implement"]


def test_a_current_review_still_creates_the_fix(q):
    """⛔The gate must not become a general brake — the normal path is the
    reason the cascade exists."""
    review_id = _review(q, reviewed_sha=NEW)

    fix_id = auto_enqueue_fix(q, review_id, head_sha_fn=_head(NEW))

    assert fix_id is not None
    assert FINDING in {t.task_id: t for t in q.list_tasks()}[fix_id].description


def test_a_stale_review_spends_no_round_and_announces_nothing(q, monkeypatch):
    """⛔Checked BEFORE the round budget. A stale review must not burn a round
    of a live lineage, and must not post "automation has stopped" about a
    finding that was already acted on."""
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "3")
    posted = []
    rid = f"review-{uuid.uuid4().hex[:8]}"
    q.enqueue(TaskRequest(task_id=rid, task_type="review", description="review",
                          branch=BRANCH,
                          context={"pr_number": PR, "reviewed_sha": OLD, "fix_round": 3}))
    q.submit_result(rid, TaskResult(task_id=rid, status="completed", summary="s",
                                    verdict="request_changes", findings=[FINDING],
                                    pr_number=PR))

    assert auto_enqueue_fix(q, rid, head_sha_fn=_head(NEW),
                            comment_fn=lambda pr, b: posted.append(b)) is None
    assert posted == []


def test_the_review_result_stays_durable_when_the_cascade_stops(q):
    """Only the follow-up work stops; the audit trail does not."""
    review_id = _review(q, reviewed_sha=OLD)

    auto_enqueue_fix(q, review_id, head_sha_fn=_head(NEW))

    result = q.get_result(review_id)
    assert result.status == "completed"
    assert result.findings == [FINDING]


# ── 3. ★★the scenario, end to end ─────────────────────────────────────


def test_two_reviews_of_one_head_with_a_fix_between_them(q):
    """★★The exact shape #253 was filed for.

    Two reviews examine commit OLD two minutes apart. The first produces a fix,
    which lands as NEW. The second — reporting the same finding, correctly, for
    the state it saw — must produce nothing.
    """
    head = {"sha": OLD}
    first = _review(q, reviewed_sha=OLD)
    second = _review(q, reviewed_sha=OLD)

    fix_id = auto_enqueue_fix(q, first, head_sha_fn=lambda pr: head["sha"])
    assert fix_id is not None, "the first review must still produce work"

    head["sha"] = NEW          # the fix lands

    assert auto_enqueue_fix(q, second, head_sha_fn=lambda pr: head["sha"]) is None
    assert len([t for t in q.list_tasks() if t.task_type == "implement"]) == 1


# ── 4. where the SHA comes from ───────────────────────────────────────


def test_worktree_prep_reports_the_commit_it_prepared(tmp_path):
    """The value has to come from the worktree that was actually checked out —
    asking git afterwards from anywhere else would answer about another tree."""
    import subprocess

    from agent_crew import server as sv

    repo = tmp_path / "wt"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=repo, check=True)
    expected = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()

    assert sv._worktree_head(str(repo)) == expected
    assert sv._worktree_head(str(tmp_path / "nope")) == ""


def test_prep_returns_the_sha_for_a_reviewer(monkeypatch, tmp_path):
    from agent_crew import server as sv

    monkeypatch.setattr(sv, "_resolve_pr_head_branch", lambda pr, *a, **k: "feat/x")
    monkeypatch.setattr(sv.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "",
                                                       "stderr": ""})())
    monkeypatch.setattr(sv, "_worktree_head", lambda path: NEW)

    got = sv._prepare_worktree_for_task(str(tmp_path), "review-abc", "main", "reviewer",
                                        task_context={"pr_number": PR})

    assert got == NEW


def test_a_prep_failure_yields_no_sha_rather_than_a_wrong_one(monkeypatch, tmp_path):
    """⛔"" means "unknown", which the gate treats as "no record" and lets
    through. Inventing a SHA here would be far worse than having none."""
    from agent_crew import server as sv

    def boom(*a, **k):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(sv, "_prepare_worktree_for_task_inner", boom)

    assert sv._prepare_worktree_for_task(str(tmp_path), "t", "main", "reviewer") == ""
