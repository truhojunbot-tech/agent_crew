"""PR #499 codex independent review r0 (REQUEST_CHANGES, 4 HIGH) — reproduce
each finding against 9a9dcff, then verify the fix on this branch.

Findings (verbatim from origin/cloud-context/496:REVIEW_499_r0.md):

1. server.py:3634 / claude_cloud.py:533 — Cloud reconciliation has no
   production caller; tasks never complete or enter independent review and
   the bounded slots fill.
2. server.py:3634 — Production Cloud dispatch omits repository identity
   although Cloud sessions do not inherit cwd; pass the registered repo into
   the prompt.
3. claude_cloud.py:473 — No-PR ALREADY_FIXED/BLOCKED_FOR_CLOUD/NEEDS_DECISION
   without a pushed commit cannot be observed and remains in_progress
   forever.
4. claude_cloud.py:494 — Task completes before independent review is
   enqueued; an enqueue exception is swallowed and cannot be retried by
   in-progress reconciliation.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from agent_crew import claude_cloud as cc
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


@pytest.fixture
def q(tmp_db):
    return TaskQueue(tmp_db)


@pytest.fixture(autouse=True)
def _cloud_enabled(monkeypatch):
    monkeypatch.setenv(cc._ENV_ENABLED, "1")


def _enqueue_implement(q, task_id=None, branch=None, context=None):
    task_id = task_id or f"impl-{uuid.uuid4().hex[:8]}"
    branch = branch or f"claude/{task_id}"
    q.enqueue(TaskRequest(task_id=task_id, task_type="implement", description="do the thing",
                          branch=branch, context=context or {}, project="agent_crew"))
    return task_id, branch


# ── Finding 1: reconciliation had no production caller ─────────────────

def test_finding1_watchdog_tick_calls_cloud_reconciliation(tmp_db, monkeypatch):
    """The watchdog loop is the EXISTING periodic trigger this server already
    runs (asyncio background task). Reconciliation must be invoked from it —
    not a new scheduler. We spy on `claude_cloud.reconcile_all_cloud_tasks`
    and drive one tick via the SAME `app.state.watchdog_tick` hook other
    tests use to test the watchdog without the asyncio loop."""
    calls = []
    import agent_crew.server as srv

    def _spy(queue, *, repo=None):
        calls.append(queue)
        return []
    monkeypatch.setattr(srv._claude_cloud, "reconcile_all_cloud_tasks", _spy)

    app = create_app(db_path=tmp_db, watchdog_disabled=True)
    with TestClient(app):
        app.state.watchdog_tick(0.0)
    assert len(calls) == 1, "the watchdog tick must call cloud reconciliation exactly once per tick"


def test_finding1_watchdog_tick_reconciles_even_with_no_pane_map(tmp_db, monkeypatch):
    """A cloud-only deployment may configure no tmux panes at all — the fix
    must not be gated behind `if not pane_map: return`, which is exactly
    where the pre-fix watchdog silently skipped every claude_cloud row."""
    calls = []
    import agent_crew.server as srv
    monkeypatch.setattr(srv._claude_cloud, "reconcile_all_cloud_tasks",
                        lambda queue, **_kw: calls.append(1) or [])
    app = create_app(db_path=tmp_db, pane_map=None, watchdog_disabled=True)
    with TestClient(app):
        app.state.watchdog_tick(0.0)
    assert calls == [1]


def test_finding1_end_to_end_pr_appears_and_review_gets_enqueued(q, monkeypatch):
    """Full reproduction with the actual reconciliation function (not a spy):
    a dispatched cloud task whose branch now has a PR must, via
    reconcile_all_cloud_tasks alone (the thing the watchdog now calls),
    complete AND enqueue a review — with no server.py/tmux involved at all."""
    task_id, branch = _enqueue_implement(q)
    q.dequeue(role="implementer")
    q.record_dispatch(task_id, channel=cc.DISPATCH_CHANNEL, agent=cc.CLOUD_PROVIDER_NAME,
                      target="pending")

    import agent_crew.github as gh
    monkeypatch.setattr(gh, "pr_number_for_branch", lambda branch, repo=None: 777)
    monkeypatch.setattr(gh, "pr_head_sha", lambda pr_number, repo=None, **_kw: "a" * 40)
    monkeypatch.setattr(gh, "pr_state", lambda pr_number, repo=None, **_kw: "open")

    outcomes = cc.reconcile_all_cloud_tasks(q)
    assert outcomes and outcomes[0].action == "pr_ready"
    completed = [t for t in q.list_tasks(status="completed") if t.task_id == task_id]
    assert completed and completed[0].pr_number == 777
    reviews = [t for t in q.list_tasks(status="pending") if t.task_type == "review"]
    assert len(reviews) == 1


# ── Finding 2: production dispatch omitted repository identity ─────────

def test_finding2_prompt_omitted_repo_before_the_fix():
    """Reproduction: the OLD call shape (`build_cloud_task_prompt(task,
    repo=repo or "")` with `repo=None` from server.py, which never passed
    one) produced a prompt with no repository line at all."""
    task = TaskRequest(task_id="t1", task_type="implement", description="x",
                       branch="claude/496", context={"repo": "truhojunbot-tech/agent_crew"})
    old_style_prompt = cc.build_cloud_task_prompt(task, repo=(None or ""))
    assert "Repository:" not in old_style_prompt, (
        "reproduction: the pre-fix call site never resolved a repo, so the "
        "prompt told the cloud session nothing about which repo to use")


def test_finding2_dispatch_resolves_repo_from_task_context(q):
    """Fix: `dispatch_cloud_for_role` now resolves the repo from the task's
    OWN `context["repo"]` (the same key `pipeline.auto_enqueue_review` and
    `_auto_enqueue_fix` already read) before building the prompt — a cloud
    session does not inherit cwd, so this must not be empty."""
    task_id, branch = _enqueue_implement(
        q, context={"repo": "truhojunbot-tech/agent_crew"})

    seen_argv = {}

    def run(argv, **_kw):
        if "--help" in argv:
            return _HelpProc()
        seen_argv["argv"] = argv
        return _LaunchProc()

    outcome = cc.dispatch_cloud_for_role(q, role="implementer", task_type="implement", run_fn=run)
    assert outcome.dispatched is True
    joined = " ".join(seen_argv["argv"])
    assert "truhojunbot-tech/agent_crew" in joined, (
        "the prompt handed to `claude --cloud` must name the repo from the "
        "task's own context, not an empty string")


def test_finding2_dispatch_falls_back_to_get_repo_when_context_has_none(q, monkeypatch):
    """When the task's context carries no repo (e.g. a bare-bones enqueue),
    fall back to `github.get_repo()` — the same cwd-based resolution every
    other call site in this codebase already uses — rather than silently
    omitting the repo line."""
    task_id, branch = _enqueue_implement(q, context={})
    import agent_crew.claude_cloud as cc_module
    monkeypatch.setattr(cc_module._github, "get_repo", lambda *a, **kw: "fallback/repo")

    seen_argv = {}

    def run(argv, **_kw):
        if "--help" in argv:
            return _HelpProc()
        seen_argv["argv"] = argv
        return _LaunchProc()

    cc.dispatch_cloud_for_role(q, role="implementer", task_type="implement", run_fn=run)
    assert "fallback/repo" in " ".join(seen_argv["argv"])


# ── Finding 3: no-PR terminal outcomes with no pushed commit hang forever ──

def test_finding3_no_pr_no_commit_stays_dispatched_within_budget(q, monkeypatch):
    monkeypatch.setenv(cc._ENV_STALE_SECONDS, "3600")
    task_id, branch = _enqueue_implement(q)
    q.dequeue(role="implementer")
    q.record_dispatch(task_id, channel=cc.DISPATCH_CHANNEL, agent=cc.CLOUD_PROVIDER_NAME,
                      target="pending")
    task = [t for t in q.list_tasks(status="in_progress") if t.task_id == task_id][0]

    outcome = cc.reconcile_cloud_dispatch(
        q, task,
        pr_number_for_branch_fn=lambda branch, repo=None: None,
        commit_message_fn=lambda branch, repo=None: None,
    )
    assert outcome.action == "still_dispatched"
    assert [t for t in q.list_tasks(status="in_progress") if t.task_id == task_id]


def test_finding3_reproduction_hangs_forever_without_a_budget(q):
    """Reproduction of the pre-fix behavior: with no staleness budget applied
    (simulated here by an effectively infinite budget), a no-PR/no-commit
    dispatch reconciles to `still_dispatched` no matter how old it is —
    exactly the 'remains in_progress forever' the review flagged."""
    import os
    old = os.environ.pop(cc._ENV_STALE_SECONDS, None)
    try:
        os.environ[cc._ENV_STALE_SECONDS] = str(10 ** 9)
        task_id, branch = _enqueue_implement(q)
        q.dequeue(role="implementer")
        q.record_dispatch(task_id, channel=cc.DISPATCH_CHANNEL, agent=cc.CLOUD_PROVIDER_NAME,
                          target="pending")
        task = [t for t in q.list_tasks(status="in_progress") if t.task_id == task_id][0]
        outcome = cc.reconcile_cloud_dispatch(
            q, task,
            pr_number_for_branch_fn=lambda branch, repo=None: None,
            commit_message_fn=lambda branch, repo=None: None,
        )
        assert outcome.action == "still_dispatched"
        assert [t for t in q.list_tasks(status="in_progress") if t.task_id == task_id], (
            "reproduction: with a huge/no budget this hangs exactly as the review described")
    finally:
        if old is None:
            os.environ.pop(cc._ENV_STALE_SECONDS, None)
        else:
            os.environ[cc._ENV_STALE_SECONDS] = old


def test_finding3_stale_dispatch_resolves_to_needs_human(q, monkeypatch):
    """Fix: past the configured budget, a no-PR/no-commit dispatch is
    resolved to `needs_human` — freeing the concurrency slot and leaving a
    human-actionable record instead of hanging forever."""
    monkeypatch.setenv(cc._ENV_STALE_SECONDS, "1")
    task_id, branch = _enqueue_implement(q)
    q.dequeue(role="implementer")
    q.record_dispatch(task_id, channel=cc.DISPATCH_CHANNEL, agent=cc.CLOUD_PROVIDER_NAME,
                      target="pending")
    # Backdate dispatched_at past the 1-second budget without sleeping.
    conn = q._connect()
    try:
        conn.execute("UPDATE tasks SET dispatched_at = ? WHERE task_id = ?",
                     (0.0, task_id))
        conn.commit()
    finally:
        conn.close()
    task = [t for t in q.list_tasks(status="in_progress") if t.task_id == task_id][0]

    outcome = cc.reconcile_cloud_dispatch(
        q, task,
        pr_number_for_branch_fn=lambda branch, repo=None: None,
        commit_message_fn=lambda branch, repo=None: None,
    )
    assert outcome.action == "needs_decision"
    resolved = [t for t in q.list_tasks(status="needs_human") if t.task_id == task_id]
    assert len(resolved) == 1, "the slot must be freed — the task must leave in_progress"
    assert not [t for t in q.list_tasks(status="in_progress") if t.task_id == task_id]


# ── Finding 4: task completed before review enqueue confirmed ──────────

def test_finding4_reproduction_completed_with_no_review_and_no_retry(q, monkeypatch):
    """Reproduction of the PRE-FIX ordering: submit_result first, then a
    best-effort enqueue whose exception is swallowed. Once "completed", the
    task no longer appears in `list_in_progress_by_dispatch_channel`, so
    nothing will ever retry the enqueue."""
    task_id, branch = _enqueue_implement(q)
    q.dequeue(role="implementer")
    q.record_dispatch(task_id, channel=cc.DISPATCH_CHANNEL, agent=cc.CLOUD_PROVIDER_NAME,
                      target="pending")

    result = TaskResult(task_id=task_id, status="completed", summary="pr found",
                        pr_number=901, branch=branch, commit="a" * 40)
    # Reproduce the PRE-FIX order directly: mark completed, THEN attempt
    # (and fail) the review enqueue exactly as the old code path did.
    q.submit_result(task_id, result)

    def _boom(*a, **kw):
        raise RuntimeError("simulated auto_enqueue_review failure")
    import agent_crew.pipeline as pipeline_module
    monkeypatch.setattr(pipeline_module, "auto_enqueue_review", _boom)
    try:
        pipeline_module.auto_enqueue_review(q, task_id, 901)
    except RuntimeError:
        pass

    reviews = [t for t in q.list_tasks() if t.task_type == "review"]
    assert reviews == [], "reproduction: no review exists"
    still_in_progress = [t for t in q.list_tasks(status="in_progress") if t.task_id == task_id]
    assert still_in_progress == [], (
        "reproduction: the task is already completed, so it will NEVER be "
        "retried by list_in_progress_by_dispatch_channel-based reconciliation")


def test_finding4_enqueue_failure_keeps_task_dispatched_for_retry(q, monkeypatch):
    """Fix: if `auto_enqueue_review` does not confirm a review, the task must
    NOT be marked completed — it stays in_progress so the next reconciliation
    pass retries."""
    task_id, branch = _enqueue_implement(q)
    q.dequeue(role="implementer")
    q.record_dispatch(task_id, channel=cc.DISPATCH_CHANNEL, agent=cc.CLOUD_PROVIDER_NAME,
                      target="pending")
    task = [t for t in q.list_tasks(status="in_progress") if t.task_id == task_id][0]

    import agent_crew.claude_cloud as cc_module

    def _raises(*a, **kw):
        raise RuntimeError("transient failure enqueueing review")
    monkeypatch.setattr(
        "agent_crew.pipeline.auto_enqueue_review", _raises)

    outcome = cc.reconcile_cloud_dispatch(
        q, task,
        pr_number_for_branch_fn=lambda branch, repo=None: 902,
        pr_head_sha_fn=lambda pr_number, repo=None: "b" * 40,
    )
    assert outcome.action == "still_dispatched"
    assert [t for t in q.list_tasks(status="in_progress") if t.task_id == task_id], (
        "the task must remain in_progress — a completed task with a failed "
        "enqueue would never be retried"
    )
    assert not [t for t in q.list_tasks(status="completed") if t.task_id == task_id]


def test_finding4_retry_succeeds_once_enqueue_works(q):
    """The retry itself must actually work once the transient failure clears:
    a second reconciliation pass with a healthy auto_enqueue_review completes
    the task AND creates the review, and the deterministic review task id
    keeps a retry idempotent even if it had partially succeeded before."""
    task_id, branch = _enqueue_implement(q)
    q.dequeue(role="implementer")
    q.record_dispatch(task_id, channel=cc.DISPATCH_CHANNEL, agent=cc.CLOUD_PROVIDER_NAME,
                      target="pending")
    task = [t for t in q.list_tasks(status="in_progress") if t.task_id == task_id][0]

    outcome = cc.reconcile_cloud_dispatch(
        q, task,
        pr_number_for_branch_fn=lambda branch, repo=None: 903,
        pr_head_sha_fn=lambda pr_number, repo=None: "c" * 40,
        pr_state_fn=lambda pr_number, repo=None: "open",
    )
    assert outcome.action == "pr_ready"
    completed = [t for t in q.list_tasks(status="completed") if t.task_id == task_id]
    assert completed and completed[0].pr_number == 903
    reviews = [t for t in q.list_tasks() if t.task_type == "review"]
    assert len(reviews) == 1


class _HelpProc:
    returncode = 0
    stdout = (
        "Usage: claude [options]\n"
        "  --cloud                 Run this session in Claude Code Cloud\n"
    )


class _LaunchProc:
    returncode = 0
    stdout = (
        "Created cloud session: claude_cloud execution backend\n"
        "View: https://claude.ai/code/session_01KCkrmLbhobrds8fApuuoun\n"
        "Resume with: claude --teleport session_01KCkrmLbhobrds8fApuuoun\n"
    )
