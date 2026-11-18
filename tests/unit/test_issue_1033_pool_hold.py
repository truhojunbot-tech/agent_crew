"""#1033: Claude implement/fix rounds respect the Quota pool signal.

On 11-17 ``fix-review-impl-924d-r0-r1`` was auto-spawned on claude_p at 12:59Z
inside a non-Athena Claude hold. Under ``hold`` such a row is parked as
``waiting_pool`` with one ``pool_hold_parked`` event and one coordinator
notice, and resumes exactly once on a fresh valid ``open``.

Correction round (c6965464841, Quota bb 20261118t093416z): an UNKNOWN signal
(missing, malformed, stale > 60 min, or a null rule on cold start) parks with
``POOL_SIGNAL_UNKNOWN`` instead of failing open.
"""

import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_crew import pool_signal
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app

FINDING = "HIGH src/agent_crew/pipeline.py:1829 - the fix round ignores the pool hold"


def _body(rule="hold", *, action="none", reason="hold slow projection 104.2% at reset",
          age=0.0):
    generated = datetime.fromtimestamp(time.time() - age, timezone.utc).isoformat()
    return json.dumps({"schema": 1, "generated_at": generated,
                       "balance": {"action": action, "reason": f"balance {action}",
                                   "numbers": {"admissions_rule": rule,
                                               "admissions_rule_reason": reason}}})


def _signal(tmp_path, monkeypatch, rule="hold", *, raw=None, missing=False, **kw):
    """Write the Quota signal. ``raw`` writes verbatim, ``missing`` removes it."""
    path = tmp_path / "quota" / "pace_signal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if missing:
        path.unlink(missing_ok=True)
    else:
        path.write_text(raw if raw is not None else _body(rule, **kw))
    monkeypatch.setenv(pool_signal.POOL_SIGNAL_ENV, str(path))
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

def _rule(tmp_path, **kw):
    return pool_signal.read_pool_signal(state_dir=str(tmp_path / "crew"), **kw)["rule"]


def test_reader_parses_rule_and_reason(tmp_path, monkeypatch):
    _signal(tmp_path, monkeypatch, "hold")
    signal = pool_signal.read_pool_signal()
    assert signal["rule"] == "hold" and "104.2%" in signal["reason"]


def test_reader_rereads_the_file_every_call(tmp_path, monkeypatch):
    path = _signal(tmp_path, monkeypatch, "hold")
    assert _rule(tmp_path) == "hold"
    path.write_text(_body("open"))
    assert _rule(tmp_path) == "open"
    path.write_text(_body("hold"))
    assert _rule(tmp_path) == "hold"


@pytest.mark.parametrize("action, rule, expected", [
    ("hold_admissions", "open", "hold"),
    ("reopen_admissions", "hold", "open"),
    ("hold_admissions", None, "hold"),
    ("none", "open", "open"),
    ("hold_cloud", "hold", "hold"),
])
def test_action_wins_over_rule(tmp_path, monkeypatch, action, rule, expected):
    _signal(tmp_path, monkeypatch, rule, action=action)
    assert _rule(tmp_path) == expected


@pytest.mark.parametrize("case", ["missing", "not-json", "empty", "no-balance",
                                  "no-generated-at", "bad-generated-at",
                                  "naive-generated-at", "bad-rule", "stale", "future"])
def test_missing_malformed_or_stale_is_unknown(tmp_path, monkeypatch, case):
    raw = {
        "not-json": "{not json", "empty": "",
        "no-balance": json.dumps({"generated_at": "2026-11-18T09:30:03+00:00"}),
        "no-generated-at": json.dumps({"balance": {"numbers": {"admissions_rule": "open"}}}),
        "bad-generated-at": _body("open").replace('"generated_at": "', '"generated_at": "x'),
        "naive-generated-at": json.dumps({"generated_at": "2026-11-18T09:30:03",
                                          "balance": {"numbers": {"admissions_rule": "open"}}}),
        "bad-rule": _body("maybe"),
        "stale": _body("open", age=61 * 60),
        "future": _body("open", age=-30 * 60),
    }.get(case)
    _signal(tmp_path, monkeypatch, raw=raw, missing=case == "missing")
    signal = pool_signal.read_pool_signal(state_dir=str(tmp_path / "crew"))
    assert signal["rule"] == "unknown"
    assert signal["reason"].startswith("POOL_SIGNAL_UNKNOWN")


def test_fresh_within_bound_is_valid(tmp_path, monkeypatch):
    _signal(tmp_path, monkeypatch, "open", age=59 * 60)
    assert _rule(tmp_path) == "open"


def test_null_rule_keeps_last_valid_state(tmp_path, monkeypatch):
    path = _signal(tmp_path, monkeypatch, None)
    assert _rule(tmp_path) == "unknown"   # cold start
    path.write_text(_body("hold"))
    assert _rule(tmp_path) == "hold"
    path.write_text(_body(None))
    assert _rule(tmp_path) == "hold"
    path.write_text(_body("open"))
    assert _rule(tmp_path) == "open"
    path.write_text(_body(None))
    assert _rule(tmp_path) == "open"
    # Stale file: the last valid state does not rescue it.
    path.write_text(_body(None, age=2 * 3600))
    assert _rule(tmp_path) == "unknown"
    assert pool_signal.load_last_valid(str(tmp_path / "crew"))["rule"] == "open"


def test_unknown_never_overwrites_last_valid_state(tmp_path, monkeypatch):
    path = _signal(tmp_path, monkeypatch, "hold")
    assert _rule(tmp_path) == "hold"
    path.write_text(_body("open", age=3 * 3600))
    assert _rule(tmp_path) == "unknown"
    path.unlink()
    assert _rule(tmp_path) == "unknown"
    assert pool_signal.load_last_valid(str(tmp_path / "crew"))["rule"] == "hold"


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
        path.write_text(_body("open"))
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


# ── UNKNOWN signal parks (c6965464841) ────────────────────────────────────

def _assert_parked_unknown(queue, task_id, dispatched, notices):
    assert dispatched == []
    parked = _events(queue, task_id, "pool_hold_parked")
    assert len(parked) == 1, parked
    assert parked[0]["rule"] == "unknown" and parked[0]["code"] == "POOL_SIGNAL_UNKNOWN"
    assert parked[0]["reason"].startswith("POOL_SIGNAL_UNKNOWN")
    assert [n[:2] for n in notices] == [(task_id, "pool_hold_parked")]
    assert "POOL_SIGNAL_UNKNOWN" in notices[0][2]


@pytest.mark.parametrize("case", ["missing", "malformed", "stale"])
def test_unknown_signal_parks_with_zero_claude_dispatch(tmp_path, monkeypatch, notices, case):
    _signal(tmp_path, monkeypatch, "open", missing=case == "missing",
            raw="{not json" if case == "malformed" else None,
            age=61 * 60 if case == "stale" else 0.0)
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    task_id = _impl(queue)
    with TestClient(app):
        assert _wait(lambda: queue.get_task_status(task_id) == "waiting_pool")
        time.sleep(0.3)
    _assert_parked_unknown(queue, task_id, dispatched, notices)


def test_cold_start_null_rule_parks(tmp_path, monkeypatch, notices):
    _signal(tmp_path, monkeypatch, None)
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    task_id = _impl(queue)
    with TestClient(app):
        assert _wait(lambda: queue.get_task_status(task_id) == "waiting_pool")
        time.sleep(0.3)
    _assert_parked_unknown(queue, task_id, dispatched, notices)


def test_last_valid_hold_then_null_stays_parked(tmp_path, monkeypatch, notices):
    path = _signal(tmp_path, monkeypatch, "hold")
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    task_id = _impl(queue)
    with TestClient(app):
        assert _wait(lambda: queue.get_task_status(task_id) == "waiting_pool")
        path.write_text(_body(None))
        time.sleep(0.4)
        assert queue.get_task_status(task_id) == "waiting_pool"
        second = _impl(queue, "impl-1033-second")
        assert _wait(lambda: queue.get_task_status(second) == "waiting_pool")
        time.sleep(0.2)
    assert dispatched == []
    assert _events(queue, task_id, "pool_hold_resumed") == []


def test_last_valid_hold_then_unreadable_stays_parked(tmp_path, monkeypatch, notices):
    path = _signal(tmp_path, monkeypatch, "hold")
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    task_id = _impl(queue)
    with TestClient(app):
        assert _wait(lambda: queue.get_task_status(task_id) == "waiting_pool")
        path.write_text("{not json")
        time.sleep(0.4)
    assert dispatched == [] and queue.get_task_status(task_id) == "waiting_pool"
    assert _events(queue, task_id, "pool_hold_resumed") == []


def test_last_valid_open_then_fresh_null_dispatches(tmp_path, monkeypatch, notices):
    path = _signal(tmp_path, monkeypatch, "open")
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    with TestClient(app):
        assert _wait(lambda: pool_signal.load_last_valid(str(tmp_path)) is not None)
        path.write_text(_body(None))
        task_id = _impl(queue)
        assert _wait(lambda: dispatched == [task_id])
    assert _events(queue, task_id, "pool_hold_parked") == []
    assert notices == []


def test_action_hold_overrides_rule_open_then_fresh_open_resumes_once(
        tmp_path, monkeypatch, notices):
    path = _signal(tmp_path, monkeypatch, "open", action="hold_admissions")
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    task_id = _impl(queue)
    with TestClient(app):
        assert _wait(lambda: queue.get_task_status(task_id) == "waiting_pool")
        time.sleep(0.3)
        assert dispatched == []
        assert _events(queue, task_id, "pool_hold_parked")[0]["rule"] == "hold"
        path.write_text(_body("open"))
        assert _wait(lambda: dispatched == [task_id])
        time.sleep(0.3)
    assert dispatched == [task_id]
    assert len(_events(queue, task_id, "pool_hold_resumed")) == 1
    assert len(notices) == 1


def test_unknown_then_fresh_open_resumes_exactly_once(tmp_path, monkeypatch, notices):
    path = _signal(tmp_path, monkeypatch, missing=True)
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    task_id = _impl(queue)
    with TestClient(app):
        assert _wait(lambda: queue.get_task_status(task_id) == "waiting_pool")
        time.sleep(0.2)
        assert dispatched == []
        path.write_text(_body("open", age=61 * 60))   # stale open: no authorisation
        time.sleep(0.3)
        assert dispatched == [] and queue.get_task_status(task_id) == "waiting_pool"
        path.write_text(_body("open"))
        assert _wait(lambda: dispatched == [task_id])
        time.sleep(0.3)
    assert dispatched == [task_id]
    assert len(_events(queue, task_id, "pool_hold_resumed")) == 1
    assert len(_events(queue, task_id, "pool_hold_parked")) == 1


@pytest.mark.parametrize("last_valid, expect_dispatch", [("open", True), ("hold", False)])
def test_last_valid_state_survives_restart(tmp_path, monkeypatch, notices,
                                           last_valid, expect_dispatch):
    path = _signal(tmp_path, monkeypatch, last_valid)
    app, queue, _dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    with TestClient(app):
        assert _wait(lambda: (pool_signal.load_last_valid(str(tmp_path)) or {}).get(
            "rule") == last_valid)
    # Restart: a fresh app (and process state) over the same state dir, with
    # Quota now writing a null rule ("no change").
    path.write_text(_body(None))
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL)
    task_id = _impl(queue)
    with TestClient(app):
        if expect_dispatch:
            assert _wait(lambda: dispatched == [task_id])
        else:
            assert _wait(lambda: queue.get_task_status(task_id) == "waiting_pool")
            time.sleep(0.3)
            assert dispatched == []
            assert _events(queue, task_id, "pool_hold_parked")[0]["rule"] == "hold"


def test_athena_under_unknown_dispatches(tmp_path, monkeypatch, notices):
    _signal(tmp_path, monkeypatch, missing=True)
    app, queue, dispatched = _app(tmp_path, monkeypatch, CLAUDE_IMPL, project="athena")
    task_id = _impl(queue, project="athena")
    with TestClient(app):
        assert _wait(lambda: dispatched == [task_id])
    assert _events(queue, task_id, "pool_hold_parked") == []
    assert notices == []


def test_codex_implement_and_claude_review_under_unknown_dispatch(tmp_path, monkeypatch,
                                                                  notices):
    _signal(tmp_path, monkeypatch, raw="{not json")
    app, queue, dispatched = _app(
        tmp_path, monkeypatch, {"implementer": "codex", "reviewer": "claude", "tester": "script"})
    task_id = _impl(queue)
    queue.enqueue(TaskRequest("review-1033", "review", "review", branch="main",
                              project="agent_crew",
                              context={"risk_tier": 1, "pr_number": 1033,
                                       "reviewed_sha": "a" * 40}))
    with TestClient(app):
        assert _wait(lambda: task_id in dispatched and "review-1033" in dispatched)
    assert notices == []


def test_chatgpt_review_under_unknown_dispatches(tmp_path, monkeypatch, notices):
    from agent_crew import chatgpt_review as chatgpt
    from tests.unit.test_issue_1000_chatgpt_reviewer import (
        FakeGh, PR, REPO, ROLES, _enqueue)
    _signal(tmp_path, monkeypatch, missing=True)
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
        path.write_text(_body("open"))
        assert _wait(lambda: dispatched == [fix_id])
        time.sleep(0.3)
    assert dispatched == [fix_id]
    assert len(_events(queue, fix_id, "pool_hold_resumed")) == 1
    assert len(_events(queue, fix_id, "pool_hold_parked")) == 1
    assert len(notices) == 1
