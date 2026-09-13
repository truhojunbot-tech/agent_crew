"""#301 — a review that never ran is not a review that rejected.

Measured on this host 2026-09-13 12:38–12:53 KST. A quota-ops branch was
dispatched on the agent_crew queue; the reviewer's worktree has no such branch,
so every review exited 1 without a verdict. Five implement dispatches and four
failed reviews in fifteen minutes, zero changes requested, zero changes needed —
the deliverable was byte-identical throughout:

    impl-7b107cef    implement  completed    12:38:06
    review-f20ceaf8  review     failed       12:49:02  {"reason": "exit_1", …}
    impl-97a68b18    implement  completed    12:49:09
    review-21141d30  review     failed       12:50:29  {"reason": "exit_1", …}
    impl-8837285a    implement  needs_human  12:50:34
    review-f3cdffe9  review     failed       12:51:58  {"reason": "exit_1", …}
    impl-44cb468b    implement  needs_human  12:52:06
    review-24669f71  review     failed       12:53:…   {"reason": "exit_1", …}

Three defects, each tested separately below:

1. `_resolve_verdict` maps ANY non-completed status to `request_changes`, so a
   reviewer that crashed is indistinguishable from a reviewer that rejected.
   `build_feedback` then produces the header and nothing else, because there are
   no findings — the literal string `"Review feedback (task <id>):"` observed in
   every re-dispatch. An implementer that takes that at face value re-implements
   correct work: #253's failure mode, reached with no finding in play at all.

2. The same conflation on the server's own cascade (`_auto_enqueue_fix`).

3. Reviewer/tester worktree prep falls back to `origin/main` with a warning when
   the task's branch does not resolve here. #289 already refused exactly this
   for a PR head that cannot be resolved; a branch that resolves nowhere in this
   repo is the same hazard with a shorter path to it.

⛔The round cap does not help. `AGENT_CREW_REVIEW_FIX_MAX_ROUNDS` bounds `fix`
  lineages, and these were never fix rounds — no review ever returned
  `request_changes`, because no review ever returned anything.
"""

import pytest

from agent_crew.loop import build_feedback, handle_review_result
from agent_crew.protocol import TaskResult


def _review(status="failed", verdict=None, findings=None, task_id="review-f20ceaf8"):
    return TaskResult(task_id=task_id, status=status, summary="",
                      verdict=verdict, findings=findings if findings is not None else [])


# ── 1. the coordinator's decision ─────────────────────────────────────


def test_a_failed_review_is_not_a_rejection():
    """★★The reported bug. `exit_1` became `request_changes` became a new
    implement task carrying no findings."""
    assert handle_review_result(_review(status="failed"), iteration=1, max_iter=3) \
        == "review_failed"


@pytest.mark.parametrize("status", ["failed", "timed_out", "needs_human", "blocked"])
def test_no_status_other_than_completed_speaks_for_the_reviewer(status):
    assert handle_review_result(_review(status=status), iteration=1, max_iter=3) \
        == "review_failed"


def test_a_real_rejection_still_requests_changes():
    """⛔The control. #244's auto-fix cascade is the whole reason the reject path
    exists; a fix that silenced it would trade one waste for a worse one."""
    outcome = handle_review_result(
        _review(status="completed", verdict="request_changes",
                findings=[{"layer": "code_quality", "issue": "real problem"}]),
        iteration=1, max_iter=3)
    assert outcome == "request_changes"


def test_an_approval_is_untouched():
    assert handle_review_result(_review(status="completed", verdict="approve"),
                                iteration=1, max_iter=3) == "approved"


def test_a_clean_review_with_nothing_to_say_is_still_an_approval():
    """#208: reviewers post verdict=None with no findings when they have nothing
    to flag. That is an approval and must not be swept into the new outcome."""
    assert handle_review_result(_review(status="completed", verdict=None, findings=[]),
                                iteration=1, max_iter=3) == "approved"


def test_a_failed_review_does_not_consume_an_escalation_round():
    """⛔A crashed reviewer must not push the lineage toward the max-iteration
    escalation either — that would convert an infrastructure fault into a
    verdict about the work."""
    assert handle_review_result(_review(status="failed"), iteration=99, max_iter=3) \
        == "review_failed"


def test_the_empty_feedback_string_is_exactly_what_was_observed():
    """Pins the symptom so the regression is recognisable: with no findings,
    `build_feedback` yields the bare header seen in every re-dispatch."""
    assert build_feedback(_review()) == "Review feedback (task review-f20ceaf8):"


# ── 2. the server's own cascade ───────────────────────────────────────


def test_the_server_does_not_enqueue_a_fix_for_a_review_that_failed():
    """The same conflation lives on the result-submission path, where a failed
    review that DOES post a result would enqueue a fix carrying no findings."""
    from agent_crew.server import _review_result_is_actionable

    assert _review_result_is_actionable(_review(status="failed")) is False
    assert _review_result_is_actionable(
        _review(status="completed", verdict="request_changes",
                findings=[{"layer": "code_quality", "issue": "x"}])) is True


# ── 3. prep must not review a branch this repo does not have ──────────


def _prepare(role, branch, *, resolvable):
    """Run prep with every git call stubbed; `resolvable` lists refs that exist."""
    from unittest.mock import MagicMock, patch

    from agent_crew.server import _prepare_worktree_for_task

    cmds = []

    def fake_run(cmd, **_kw):
        cmds.append(cmd)
        if "rev-parse" in cmd and "--verify" in cmd:
            ref = cmd[-1].replace("^{commit}", "")
            if ref in resolvable:
                return MagicMock(returncode=0, stdout="a" * 40 + "\n", stderr="")
            return MagicMock(returncode=1, stdout="", stderr="unknown revision")
        if "rev-parse" in cmd:
            return MagicMock(returncode=0, stdout="a" * 40 + "\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("agent_crew.server.subprocess.run", side_effect=fake_run):
        _prepare_worktree_for_task("/wt/codex", "review-f20ceaf8", branch, role)
    return cmds


@pytest.mark.parametrize("role", ["reviewer", "tester"])
def test_a_branch_this_repo_does_not_have_is_refused(role):
    """★★The crash, at its cause. `fix/idle-gate-hard-floor-alert-49` exists only
    in quota-ops. Prep resolved `origin/main` from the fallback list and prepared
    the reviewer there — so the review was about main, not about the task — and
    said so in a warning nobody reads. #289 refused exactly this for an
    unresolvable PR head; the argument does not change because the name arrived
    as a branch instead."""
    from agent_crew.server import WorktreeTargetUnresolved

    with pytest.raises(WorktreeTargetUnresolved) as exc:
        _prepare(role, "fix/idle-gate-hard-floor-alert-49",
                 resolvable={"origin/main", "main"})
    assert "fix/idle-gate-hard-floor-alert-49" in str(exc.value)


@pytest.mark.parametrize("role", ["reviewer", "tester"])
def test_a_branch_that_does_resolve_is_prepared_normally(role):
    """⛔The control. Refusing must key on "this repo does not have it", not on
    "reviewing a branch", or every review stops working."""
    cmds = _prepare(role, "feat/real-branch",
                    resolvable={"origin/feat/real-branch", "origin/main", "main"})
    assert any("checkout" in c and "--detach" in c for c in cmds)


def test_the_implementer_may_still_name_a_branch_that_does_not_exist_yet():
    """⛔The other control, and the reason this is scoped to reviewer/tester. An
    implementer routinely names a branch nobody has created yet — that is what
    it is for. Refusing there would break every new feature branch."""
    cmds = _prepare("implementer", "agent/brand-new", resolvable={"origin/main", "main"})
    assert cmds, "implementer prep raised instead of falling back"


# ── 4. the loop that actually runs ────────────────────────────────────


def test_both_cli_loops_stop_instead_of_re_implementing():
    """⛔The fix is inert unless the callers honour the new outcome. Both loops
    fall through to `enqueue_implement(...context={"feedback": ...})` for any
    outcome they do not recognise, so a new outcome nobody checks would keep
    the cascade running exactly as measured."""
    import inspect

    from agent_crew import cli

    source = inspect.getsource(cli)
    assert source.count('outcome == "review_failed"') == 2, (
        "each review loop must stop on a review that did not run; found "
        f"{source.count('outcome == \"review_failed\"')} guard(s)")


def test_the_new_outcome_is_not_silently_one_of_the_old_ones():
    """`review_failed` has to be distinguishable from `request_changes` at the
    call site, or the guard above would be checking a string that never
    occurs."""
    outcomes = {
        handle_review_result(_review(status=s), iteration=1, max_iter=3)
        for s in ("failed", "timed_out", "needs_human", "blocked")
    }
    assert outcomes == {"review_failed"}
    assert handle_review_result(
        _review(status="completed", verdict="request_changes",
                findings=[{"layer": "code_quality", "issue": "x"}]),
        iteration=1, max_iter=3) == "request_changes"
