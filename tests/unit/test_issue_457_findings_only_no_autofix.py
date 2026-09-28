"""#457 — findings-only reviews must not trigger automated fix rounds.

Observed 2026-09-27 11:16Z: a red-team review on branch ``main`` (findings
only, explicitly no fixes) returned ``request_changes`` and the #244 cascade
dispatched ``fix-redteam-51-broker-boundary-r1`` to codex on ``main``. The only
opt-out was the global ``AGENT_CREW_REVIEW_FIX_MAX_ROUNDS``.

  * ``context.findings_only`` suppresses the review→fix transition per task;
  * a review of the default branch never auto-creates a fix task;
  * an eligible feature-branch review keeps the existing bounded behaviour.
"""

import subprocess
import uuid

import pytest

from agent_crew.pipeline import auto_enqueue_fix, fix_task_id
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue

FEATURE = "agent/codex/457-x"
FINDING = "HIGH broker.py:51 - boundary check missing"


def _open(pr):
    return "open"


@pytest.fixture
def q(tmp_db, monkeypatch):
    monkeypatch.delenv("AGENT_CREW_MAIN_BRANCH", raising=False)
    monkeypatch.delenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", raising=False)
    return TaskQueue(tmp_db)


def _review(q, *, branch=FEATURE, context=None, pr_number=None):
    review_id = f"review-{uuid.uuid4().hex[:8]}"
    ctx = dict(context or {})
    if pr_number is not None:
        ctx["pr_number"] = pr_number
    q.enqueue(TaskRequest(task_id=review_id, task_type="review",
                          description="review", branch=branch, context=ctx))
    q.submit_result(review_id, TaskResult(
        task_id=review_id, status="completed", summary="request_changes",
        verdict="request_changes", findings=[FINDING], pr_number=pr_number))
    return review_id


def _implement_ids(q):
    return [t.task_id for t in q.list_tasks() if t.task_type == "implement"]


# ── 1. findings-only / no-fix review ─────────────────────────────────────


def test_findings_only_review_enqueues_no_fix(q):
    review_id = _review(q, context={"findings_only": True}, pr_number=51)

    assert auto_enqueue_fix(q, review_id, pr_state_fn=_open) is None
    assert _implement_ids(q) == []
    # The review's findings are still on record — only the cascade stops.
    assert q.get_result(review_id).findings == [FINDING]


def test_findings_only_review_does_not_announce_budget_exhaustion(q, monkeypatch):
    """An exhausted budget would normally post a PR comment; a findings-only
    review must not reach that path either."""
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "1")
    posted = []

    def run(pr, context):
        review_id = _review(q, context=context, pr_number=pr)
        return auto_enqueue_fix(q, review_id, pr_state_fn=_open,
                                already_announced_fn=lambda pr, marker: False,
                                comment_fn=lambda pr, body: posted.append(pr),
                                repo="owner/repo")

    assert run(51, {"findings_only": True, "fix_round": 1}) is None
    assert posted == []
    # Control: the same exhausted review without the flag does announce.
    assert run(52, {"fix_round": 1}) is None
    assert posted == [52]


def test_findings_only_false_keeps_normal_behaviour(q):
    review_id = _review(q, context={"findings_only": False})

    assert auto_enqueue_fix(q, review_id, pr_state_fn=_open) == fix_task_id(review_id, 1)


# ── 2. review of the default branch ──────────────────────────────────────


@pytest.mark.parametrize("branch", ["main", "origin/main", "refs/heads/main"])
def test_default_branch_review_enqueues_no_fix(q, branch):
    """The #457 incident shape: no findings_only flag, branch main."""
    review_id = _review(q, branch=branch)

    assert auto_enqueue_fix(q, review_id, pr_state_fn=_open) is None
    assert _implement_ids(q) == []


def test_configured_main_branch_is_the_default(q, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_MAIN_BRANCH", "develop")
    review_id = _review(q, branch="develop")

    assert auto_enqueue_fix(q, review_id, pr_state_fn=_open) is None


def test_task_base_branch_is_never_a_fix_target(q):
    review_id = _review(q, branch="integration", context={"base_branch": "integration"})

    assert auto_enqueue_fix(q, review_id, pr_state_fn=_open) is None


def test_remote_default_branch_from_origin_head(q, tmp_path):
    """A repository whose default is not ``main`` is still recognised when a
    checkout is available to ask."""
    repo = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "root"], check=True)
    subprocess.run([*git, "update-ref", "refs/remotes/origin/trunk", "HEAD"], check=True)
    subprocess.run(["git", "-C", str(repo), "symbolic-ref",
                    "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk"],
                   check=True)
    review_id = _review(q, branch="trunk")

    assert auto_enqueue_fix(q, review_id, pr_state_fn=_open,
                            repo_cwd=str(repo)) is None
    # Same checkout, feature branch: still eligible.
    other = _review(q, branch=FEATURE)
    assert auto_enqueue_fix(q, other, pr_state_fn=_open,
                            repo_cwd=str(repo)) == fix_task_id(other, 1)


# ── 3. eligible feature-branch review is unchanged ───────────────────────


def test_feature_branch_review_still_enqueues_bounded_fix(q, monkeypatch):
    review_id = _review(q, pr_number=241)

    fix_id = auto_enqueue_fix(q, review_id, pr_state_fn=_open)

    assert fix_id == fix_task_id(review_id, 1)
    fix = {t.task_id: t for t in q.list_tasks()}[fix_id]
    assert fix.task_type == "implement"
    assert fix.branch == FEATURE
    assert fix.context["fix_round"] == 1
    assert fix.context["review_findings"] == [FINDING]
    assert "(automated fix round 1/3)" in fix.description


def test_feature_branch_review_still_respects_the_round_cap(q, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", "2")
    review_id = _review(q, context={"fix_round": 2})

    assert auto_enqueue_fix(q, review_id, pr_state_fn=_open) is None
    assert _implement_ids(q) == []
