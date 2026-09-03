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


# ── 5. the lookup must name the repository ────────────────────────────
#
# The gate is fail-closed, so a lookup that structurally cannot work does not
# degrade it — it disables the entire review→fix loop. `pr_head_sha` fell back
# to `get_repo()`, which reads the process's working directory; the server runs
# in the instance directory, which belongs to a DIFFERENT repository. From
# there `get_repo()` answers `truhojunbot-tech/alfred`, so "PR 251" was asked of
# the wrong repo (review of PR #255).
#
# These exercise the DEFAULT path — no injected `head_sha_fn` — because
# injecting one skips exactly the code that was broken.


def _fake_gh(recorder, stdout):
    def run(argv, **kw):
        recorder.append({"argv": list(argv), "cwd": kw.get("cwd")})

        class R:
            returncode = 0
        R.stdout = stdout
        R.stderr = ""
        return R()
    return run


def test_the_head_lookup_names_the_repo_from_the_review_context(monkeypatch, q):
    """★No `head_sha_fn`: the real `gh` argv is inspected."""
    import agent_crew.github as gh

    calls = []
    monkeypatch.setattr(gh, "check_gh_installed", lambda: True)
    monkeypatch.setattr(gh.subprocess, "run",
                        _fake_gh(calls, '{"commits":[{"oid":"%s"}]}' % NEW))

    rid = f"review-{uuid.uuid4().hex[:8]}"
    q.enqueue(TaskRequest(task_id=rid, task_type="review", description="review",
                          branch=BRANCH,
                          context={"pr_number": PR, "reviewed_sha": OLD,
                                   "repo": "truhojunbot-tech/agent_crew"}))
    q.submit_result(rid, TaskResult(task_id=rid, status="completed", summary="s",
                                    verdict="request_changes", findings=[FINDING],
                                    pr_number=PR))

    assert auto_enqueue_fix(q, rid) is None, "a superseded review still created work"

    assert calls, "no gh call was made — the default lookup path was skipped"
    argv = calls[0]["argv"]
    assert "--repo" in argv and "truhojunbot-tech/agent_crew" in argv, \
        f"gh was not told which repository to ask: {argv}"


def test_a_worktree_supplies_the_repo_when_the_context_does_not(monkeypatch, q):
    """The server passes a checkout; `gh` must be asked from inside it."""
    import agent_crew.github as gh

    calls = []
    monkeypatch.setattr(gh, "check_gh_installed", lambda: True)
    monkeypatch.setattr(gh.subprocess, "run",
                        _fake_gh(calls, "git@github.com:org/repo.git\n"))

    from agent_crew.pipeline import review_is_current

    review_is_current({"reviewed_sha": OLD}, PR, repo_cwd="/wt/claude")

    assert calls[0]["cwd"] == "/wt/claude", \
        "repo detection ran in the process cwd instead of the given checkout"


def test_no_known_repo_lets_the_cascade_proceed(q, caplog):
    """⛔The one place this gate must NOT be fail-closed.

    "We could not reach GitHub" is a transient state worth deferring on. "No
    repository is configured" is not a state at all — it never resolves, and
    deferring on it would silently disable every review→fix cascade, which is a
    far worse failure than the duplicate work the gate prevents.
    """
    import logging

    from agent_crew.pipeline import review_is_current

    with caplog.at_level(logging.WARNING, logger="agent_crew.pipeline"):
        current, why = review_is_current({"reviewed_sha": OLD}, PR)

    assert current is True and "no repo" in why
    assert any("no repo known" in r.message for r in caplog.records), \
        "the cascade proceeded without saying why"


def test_a_reachable_repo_with_an_unreadable_head_still_defers(q):
    """Transient failure keeps the conservative behaviour."""
    from agent_crew.pipeline import review_is_current

    current, why = review_is_current(
        {"reviewed_sha": OLD, "repo": "org/repo"}, PR, head_sha_fn=lambda pr: "")

    assert current is False and "unknown" in why
