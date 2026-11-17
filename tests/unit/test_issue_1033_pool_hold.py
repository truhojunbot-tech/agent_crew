"""#1033: Claude implement/fix rounds respect the Quota pool signal.

On 11-17 ``fix-review-impl-924d-r0-r1`` was auto-spawned on claude_p at 12:59Z
inside a non-Athena Claude hold. Under ``hold`` such a row is parked as
``waiting_pool`` with one ``pool_hold_parked`` event and one coordinator
notice, and resumes exactly once when the signal reads ``open``.
"""

import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_crew import pool_signal
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app

FINDING = "HIGH src/agent_crew/pipeline.py:1829 - the fix round ignores the pool hold"


def _signal(tmp_path, monkeypatch, rule, reason="hold slow projection 104.2% at reset"):
    path = tmp_path / "quota" / "pace_signal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if rule is None:
        path.write_text("{not json")
    else:
        path.write_text(json.dumps({"balance": {"numbers": {
            "admissions_rule": rule, "admissions_rule_reason": reason}}}))
    monkeypatch.setenv(pool_signal.POOL_SIGNAL_ENV, str(path))
    pool_signal.clear_cache()
    return path


def _state(tmp_path, roles):
    entries = []
    for role, agent in roles.items():
        worktree = tmp_path / f"wt_{role}"
        (worktree / ".git").mkdir(parents=True, exist_ok=True)
        entries.append({"role": role, "agent": agent, "worktree": str(worktree)})
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"role_agents": dict(roles), "roles": entries}))
    return str(state)


@pytest.fixture
def notices(monkeypatch):
    seen = []

    def once(queue, task_id, kind, detail="", *a, **kw):
        # Same durable claim the real helper takes: exactly one per task+kind.
        if queue.claim_coordinator_notice(task_id, kind):
            seen.append((task_id, kind, detail))

    monkeypatch.setattr("agent_crew.server.coordinator_notice_once", once)
    return seen


def _app(tmp_path, monkeypatch, roles, *, project="agent_crew"):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", "0.02")
    monkeypatch.setenv("AGENT_CREW_WORKTREE_SYNC_DISABLED", "1")
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    app = create_app(queue._db_path, state_path=_state(tmp_path, roles), project=project,
                     pane_map={}, watchdog_disabled=True, anomaly_disabled=True)
    dispatched = []

    async def fake_dispatch(task, role, **kw):
        dispatched.append(task.task_id)

    app.state.dispatch_task = fake_dispatch
    return app, queue, dispatched


def _wait(pred, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not pred():
        time.sleep(0.02)
    return pred()


def _events(queue, task_id, name):
    return [e for e in queue.get_exec_state(task_id)["events"] if e["event"] == name]


def _impl(queue, task_id="impl-1033", project="agent_crew"):
    queue.enqueue(TaskRequest(task_id, "implement", "work", branch="main",
                              project=project, context={"risk_tier": 1}))
    return task_id


CLAUDE_IMPL = {"implementer": "claude", "reviewer": "codex", "tester": "script"}


# ── signal reader ─────────────────────────────────────────────────────────

def test_reader_parses_rule_and_reason(tmp_path, monkeypatch):
    _signal(tmp_path, monkeypatch, "hold")
    assert pool_signal.read_pool_signal()["rule"] == "hold"
    assert "104.2%" in pool_signal.read_pool_signal()["reason"]


def test_reader_caches_for_a_short_window(tmp_path, monkeypatch):
    path = _signal(tmp_path, monkeypatch, "hold")
    assert pool_signal.read_pool_signal(now=100.0)["rule"] == "hold"
    path.write_text(json.dumps({"balance": {"numbers": {"admissions_rule": "open"}}}))
    assert pool_signal.read_pool_signal(now=130.0)["rule"] == "hold"
    assert pool_signal.read_pool_signal(now=161.0)["rule"] == "open"


@pytest.mark.parametrize("body", [None, "", '{"balance": {}}',
                                  '{"balance": {"numbers": {"admissions_rule": "maybe"}}}'])
def test_missing_or_malformed_signal_is_unreadable(tmp_path, monkeypatch, body):
    path = tmp_path / "sig.json"
    if body is not None:
        path.write_text(body)
    monkeypatch.setenv(pool_signal.POOL_SIGNAL_ENV, str(path))
    pool_signal.clear_cache()
    assert pool_signal.read_pool_signal()["rule"] == "unreadable"


def test_conftest_never_reads_the_live_signal():
    assert "alfred/quota" not in pool_signal.pool_signal_path()


# ── dispatcher gate ───────────────────────────────────────────────────────

def test_hold_parks_claude_implement(tmp_path, monkeypatch, notices):
    _signal(tmp_path, monkeypatch, "hold")
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    task_id = _impl(queue)
    with TestClient(app):
        assert _wait(lambda: queue.get_task_status(task_id) == "waiting_pool")
        time.sleep(0.2)
    assert dispatched == []
    parked = _events(queue, task_id, "pool_hold_parked")
    assert len(parked) == 1 and "104.2%" in parked[0]["reason"]
    assert notices == [(task_id, "pool_hold_parked",
                        "reason=hold slow projection 104.2% at reset")]


def test_open_dispatches_claude_implement(tmp_path, monkeypatch, notices):
    _signal(tmp_path, monkeypatch, "open")
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    task_id = _impl(queue)
    with TestClient(app):
        assert _wait(lambda: dispatched == [task_id])
    assert _events(queue, task_id, "pool_hold_parked") == []
    assert notices == []


def test_hold_then_open_resumes_exactly_once(tmp_path, monkeypatch, notices):
    path = _signal(tmp_path, monkeypatch, "hold")
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    task_id = _impl(queue)
    with TestClient(app):
        assert _wait(lambda: queue.get_task_status(task_id) == "waiting_pool")
        time.sleep(0.2)
        assert dispatched == []
        path.write_text(json.dumps({"balance": {"numbers": {"admissions_rule": "open"}}}))
        pool_signal.clear_cache()
        assert _wait(lambda: dispatched == [task_id])
        time.sleep(0.3)
    assert dispatched == [task_id]
    assert len(_events(queue, task_id, "pool_hold_resumed")) == 1
    assert len(_events(queue, task_id, "pool_hold_parked")) == 1
    assert len(notices) == 1


def test_athena_under_hold_dispatches(tmp_path, monkeypatch, notices):
    _signal(tmp_path, monkeypatch, "hold")
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL, project="athena")
    task_id = _impl(queue, project="athena")
    with TestClient(app):
        assert _wait(lambda: dispatched == [task_id])
    assert _events(queue, task_id, "pool_hold_parked") == []


def test_codex_implement_under_hold_dispatches(tmp_path, monkeypatch, notices):
    _signal(tmp_path, monkeypatch, "hold")
    app, queue, dispatched = _app(
        tmp_path, monkeypatch, {"implementer": "codex", "reviewer": "claude", "tester": "script"})
    task_id = _impl(queue)
    with TestClient(app):
        assert _wait(lambda: dispatched == [task_id])
    assert notices == []


def test_claude_review_under_hold_dispatches(tmp_path, monkeypatch, notices):
    _signal(tmp_path, monkeypatch, "hold")
    app, queue, dispatched = _app(
        tmp_path, monkeypatch, {"implementer": "codex", "reviewer": "claude", "tester": "script"})
    queue.enqueue(TaskRequest("review-1033", "review", "review", branch="main",
                              project="agent_crew",
                              context={"risk_tier": 1, "pr_number": 1033,
                                       "reviewed_sha": "a" * 40}))
    with TestClient(app):
        assert _wait(lambda: "review-1033" in dispatched)
    assert _events(queue, "review-1033", "pool_hold_parked") == []


def test_chatgpt_review_under_hold_dispatches(tmp_path, monkeypatch, notices):
    from agent_crew import chatgpt_review as chatgpt
    from tests.unit.test_issue_1000_chatgpt_reviewer import (
        FakeGh, PR, REPO, ROLES, _enqueue)
    _signal(tmp_path, monkeypatch, "hold")
    fake = FakeGh()
    monkeypatch.setattr(chatgpt, "GhClient", lambda *a, **kw: fake)
    monkeypatch.setenv(chatgpt._ENV_CARRIER, "pr")
    app, queue, _dispatched = _app(tmp_path, monkeypatch, dict(ROLES))
    review_id = _enqueue(queue)
    with TestClient(app):
        assert _wait(lambda: bool(queue.get_task_context(review_id).get(
            "chatgpt_request_comment_id")))
    assert queue.get_task(review_id).status == "in_progress"
    assert len(fake.posted) == 1 and fake.posted[0][:2] == (REPO, PR)
    assert _events(queue, review_id, "pool_hold_parked") == []


def test_unreadable_signal_dispatches_with_warning(tmp_path, monkeypatch, notices):
    _signal(tmp_path, monkeypatch, None)
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    task_id = _impl(queue)
    with TestClient(app):
        assert _wait(lambda: dispatched == [task_id])
    warned = _events(queue, task_id, "pool_signal_unreadable")
    assert len(warned) == 1 and warned[0]["outcome"] == "fail_open"
    assert _events(queue, task_id, "pool_hold_parked") == []


def test_stop_keeps_parked_row_parked(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    task_id = _impl(queue)
    assert queue.park_pool_hold(task_id, reason="hold")
    monkeypatch.setattr(TaskQueue, "_stop_active_in_txn", lambda self, conn, point=None: True)
    assert not queue.resume_pool_hold(task_id, rule="open")
    assert queue.get_task_status(task_id) == "waiting_pool"


def test_park_from_claim_and_resume_once(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    task_id = _impl(queue)
    assert queue.dequeue(role="implementer", agent="claude").task_id == task_id
    assert queue.park_pool_hold(task_id, reason="hold")
    assert queue.get_task_status(task_id) == "waiting_pool"
    assert not queue.park_pool_hold(task_id, reason="hold")
    assert queue.resume_pool_hold(task_id, rule="open")
    assert not queue.resume_pool_hold(task_id, rule="open")
    assert queue.dequeue(role="implementer", agent="claude").task_id == task_id


# ── acceptance 1: replay of the 11-17 924d-r0-r1 case ─────────────────────

@pytest.fixture
def open_review_pr(monkeypatch):
    monkeypatch.setattr("agent_crew.github.pr_state", lambda *a, **k: "open")
    monkeypatch.setattr("agent_crew.github.pr_head_sha", lambda *a, **k: "a" * 40)
    monkeypatch.setattr("agent_crew.github.post_review_comment", lambda *a, **k: True)
    monkeypatch.setattr("agent_crew.server.review_publication_decision",
                        lambda *a, **k: SimpleNamespace(publish=True, status="current", reason=""))


@pytest.mark.usefixtures("pinned_task_transport")
def test_replay_request_changes_fix_round_under_hold(tmp_path, monkeypatch, notices,
                                                     open_review_pr):
    path = _signal(tmp_path, monkeypatch, "hold")
    state = _state(tmp_path, CLAUDE_IMPL)
    db = str(tmp_path / "tasks.db")
    # Phase 1: the review result arrives; the cascade must park the fix at
    # its start site (dispatcher off, so only the start-site gate acts).
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    # Project identity comes from the state dir, as on the live 8105 server.
    app = create_app(db, state_path=state, pane_map={},
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app) as client:
        assert client.post("/tasks", json={
            "task_id": "review-impl-924d-r0", "task_type": "review",
            "description": "review", "branch": "agent/claude/924d", "priority": 3,
            "context": {"repo": "owner/repo", "pr_number": 1031,
                        "reviewed_sha": "a" * 40, "prev_task_id": "impl-924d"},
            "pr_number": 1031}).status_code in (200, 201)
        response = client.post("/tasks/review-impl-924d-r0/result", json={
            "task_id": "review-impl-924d-r0", "status": "completed",
            "summary": "Request changes because the fix round ignores the pool hold.",
            "verdict": "request_changes", "findings": [FINDING], "pr_number": 1031})
        assert response.status_code == 200, response.text
    queue = TaskQueue(db)
    fixes = [t for t in queue.list_tasks() if t.task_type == "implement"]
    assert len(fixes) == 1
    fix_id = fixes[0].task_id
    assert queue.get_task_status(fix_id) == "waiting_pool"
    assert len(_events(queue, fix_id, "pool_hold_parked")) == 1
    assert [n[:2] for n in notices] == [(fix_id, "pool_hold_parked")]

    # Phase 2: the dispatcher runs under hold → no Claude dispatch; then the
    # signal opens → resumed and dispatched exactly once.
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL, project=None)
    with TestClient(app):
        time.sleep(0.3)
        assert dispatched == []
        assert queue.get_task_status(fix_id) == "waiting_pool"
        path.write_text(json.dumps({"balance": {"numbers": {"admissions_rule": "open"}}}))
        pool_signal.clear_cache()
        assert _wait(lambda: dispatched == [fix_id])
        time.sleep(0.3)
    assert dispatched == [fix_id]
    assert len(_events(queue, fix_id, "pool_hold_resumed")) == 1
    assert len(_events(queue, fix_id, "pool_hold_parked")) == 1
    assert len(notices) == 1
