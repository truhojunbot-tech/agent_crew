"""#887: non-dispatcher claims carry a lease; MCP shares HTTP's result guards;
force_fail closes the attribution row.

(5) A task claimed over MCP, HTTP poll or a tmux pane got no lease. The
    watchdog's pane loop skips a row whose pane cannot be resolved, and the
    orphan sweep only looks at rows with ``lease_expires_at``, so such a task sat
    ``in_progress`` forever and kept counting toward ``max_open_implement``.
(6) MCP ``submit_result`` logged the dispatch-base-absent case instead of
    holding it under enforce, and acted on a review approval without the #304
    head check HTTP applies.
(7) ``force_fail`` ended the lease but left ``task_attribution`` in_progress.
"""

import asyncio
import sqlite3
import time

from fastapi.testclient import TestClient

from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app

from tests.unit.test_sev0_cea_s4c_dispatch_base_absent import (  # noqa: F401
    _baseless_implement, _row, hermetic_admission,
)


def _mcp_tool(tmp_db, name):
    from agent_crew.mcp_server import build_mcp_server

    fn = build_mcp_server(tmp_db)._tool_manager._tools[name].fn

    def call(**kwargs):
        return asyncio.run(fn(**kwargs)) if asyncio.iscoroutinefunction(fn) else fn(**kwargs)
    return call


def _app(tmp_db):
    return create_app(tmp_db, pane_map={}, pane_busy_fn=lambda _: False,
                      pane_liveness_fn=lambda _: "alive",
                      timeout_seconds=100000, watchdog_disabled=True,
                      anomaly_disabled=True)


def _lease(tmp_db, task_id):
    with sqlite3.connect(tmp_db) as conn:
        return conn.execute("SELECT lease_owner, lease_expires_at FROM tasks "
                            "WHERE task_id=?", (task_id,)).fetchone()


# ── (5) leases for non-dispatcher claims ──────────────────────────────


def test_mcp_claim_gets_a_lease_and_expires_as_an_orphan(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id="mcp-claim", task_type="implement",
                              description="work", branch="main"))

    handed = _mcp_tool(tmp_db, "get_next_task")(agent="codex", role="implementer")
    assert handed["task_id"] == "mcp-claim"

    owner, expires = _lease(tmp_db, "mcp-claim")
    assert owner and expires is not None, "an MCP claim must carry a lease"

    app = _app(tmp_db)
    with TestClient(app):
        actions = app.state.watchdog_tick(now=time.time() + 10 ** 7)
    assert actions["timed_out"] == ["mcp-claim"]
    assert queue.get_task_status("mcp-claim") == "timed_out"


def test_http_poll_claim_gets_a_lease(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id="poll-claim", task_type="implement",
                              description="work", branch="main"))
    with TestClient(_app(tmp_db)) as client:
        handed = client.get("/tasks/next",
                            params={"role": "implementer", "agent": "codex"}).json()
    assert handed["task_id"] == "poll-claim"
    owner, expires = _lease(tmp_db, "poll-claim")
    assert owner and expires is not None, "an HTTP-poll claim must carry a lease"


def test_live_lease_is_not_reaped(tmp_db, monkeypatch):
    """Control: a fresh lease is still within its bound."""
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id="mcp-live", task_type="implement",
                              description="work", branch="main"))
    _mcp_tool(tmp_db, "get_next_task")(agent="codex", role="implementer")
    app = _app(tmp_db)
    with TestClient(app):
        actions = app.state.watchdog_tick(now=time.time() + 60)
    assert actions["timed_out"] == []
    assert queue.get_task_status("mcp-live") == "in_progress"


def test_late_heartbeat_then_silence_still_expires(tmp_db, monkeypatch):
    """Review r0: one heartbeat after the lease ended exempted the row forever."""
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    monkeypatch.setenv("AGENT_CREW_CLAIM_LEASE_S", "10")
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id="mcp-late-hb", task_type="implement",
                              description="work", branch="main"))
    _mcp_tool(tmp_db, "get_next_task")(agent="codex", role="implementer")
    _owner, expires = _lease(tmp_db, "mcp-late-hb")
    with sqlite3.connect(tmp_db) as conn:
        conn.execute("UPDATE tasks SET last_heartbeat_at=? WHERE task_id=?",
                     (expires + 1, "mcp-late-hb"))

    app = _app(tmp_db)
    with TestClient(app):
        fresh = app.state.watchdog_tick(now=expires + 60)
        assert fresh["timed_out"] == [], "a fresh heartbeat still proves liveness"
        silent = app.state.watchdog_tick(now=expires + 100000)
    assert silent["timed_out"] == ["mcp-late-hb"]
    assert queue.get_task_status("mcp-late-hb") == "timed_out"


def test_claim_lease_env_override(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setenv("AGENT_CREW_CLAIM_LEASE_S", "120")
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id="mcp-env", task_type="implement",
                              description="work", branch="main"))
    before = time.time()
    _mcp_tool(tmp_db, "get_next_task")(agent="codex", role="implementer")
    _owner, expires = _lease(tmp_db, "mcp-env")
    assert before + 119 <= expires <= time.time() + 121


# ── (6a) MCP holds a baseless completion under enforce ────────────────


def test_mcp_under_enforce_a_baseless_completion_is_held(tmp_db, monkeypatch,
                                                        hermetic_admission):  # noqa: F811
    monkeypatch.setenv("AGENT_CREW_CEA_MODE__HOT", "test")
    queue = _baseless_implement(tmp_db, project="hot")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE__HOT", "enforce")
    with TestClient(create_app(tmp_db, pane_map={}, watchdog_disabled=True,
                               anomaly_disabled=True, worktree_map={},
                               push_fn=lambda *a, **k: None)) as client:
        handed = client.get("/tasks/next",
                            params={"role": "implementer", "agent": "claude"}).json()
        nonce = handed["dispatch_nonce"]
        assert client.post("/tasks/impl-nobase/start",
                           json={"nonce": nonce, "presenter": "claude"}).json()["go"]

    out = _mcp_tool(tmp_db, "submit_result")(
        task_id="impl-nobase", status="completed", summary="tests green",
        executor_binding={"nonce": nonce, "presenter": "claude"})

    assert out.get("held") == "no_artifact", out
    row = _row(queue, "impl-nobase")
    assert row["status"] == "failed"
    assert "dispatch base absent" in row["error_info"]["detail"]
    assert not [t for t in queue.list_tasks() if t.task_type == "review"]


# ── (6b) MCP applies the #304 head gate to an approval ────────────────

REVIEWED = "a" * 40
MOVED = "b" * 40


def _approved_review(tmp_db, monkeypatch, head):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    monkeypatch.setattr("agent_crew.github.pr_state", lambda *a, **k: "open")
    monkeypatch.setattr("agent_crew.github.pr_head_sha", lambda *a, **k: head)
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="review-887", task_type="review", description="review",
                          branch="feat/x",
                          context={"pr_number": 887, "repo": "o/r",
                                   "reviewed_sha": REVIEWED}))
    q.dequeue(role="reviewer")
    out = _mcp_tool(tmp_db, "submit_result")(
        task_id="review-887", status="completed", summary="approve: the change is correct and covered by tests",
        verdict="approve", findings=[], pr_number=887)
    return q, out


def test_mcp_stale_approval_does_not_enqueue_a_tester(tmp_db, monkeypatch):
    q, out = _approved_review(tmp_db, monkeypatch, MOVED)
    assert out.get("acknowledged") is True, out
    assert not [t for t in q.list_tasks() if t.task_type == "test"], \
        "an approval of a head that moved must not spend a tester"
    ctx = q.get_task("review-887").context
    assert ctx.get("review_publication") == "stale"


def test_mcp_stale_approval_requeues_one_review_at_the_new_head(tmp_db, monkeypatch):
    """Review r0: HTTP requeues a head-anchored review; MCP stranded the lineage."""
    from agent_crew.pipeline import stale_review_task_id

    q, out = _approved_review(tmp_db, monkeypatch, MOVED)
    assert out.get("acknowledged") is True, out
    successor = q.get_task(stale_review_task_id(887, MOVED))
    assert successor is not None, "a stale approval must requeue a new-head review"
    assert successor.task_type == "review"
    assert successor.context["expected_head_sha"] == MOVED
    assert successor.context["superseded_review"] == "review-887"

    # Idempotent: the same moved head never puts two reviewers on one commit.
    from agent_crew.pipeline import requeue_review_at_head
    again = requeue_review_at_head(q, "review-887", 887, MOVED,
                                   q.get_task("review-887").context, project="")
    assert again == successor.task_id
    assert len([t for t in q.list_tasks() if t.task_type == "review"]) == 2


def test_mcp_current_approval_still_enqueues_a_tester(tmp_db, monkeypatch):
    """Control."""
    q, out = _approved_review(tmp_db, monkeypatch, REVIEWED)
    assert out.get("acknowledged") is True, out
    assert [t for t in q.list_tasks() if t.task_type == "test"]


# ── (7) force_fail closes the attribution ─────────────────────────────


def test_force_fail_closes_attribution(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    q = TaskQueue(tmp_db)
    q.enqueue(TaskRequest(task_id="ff", task_type="implement",
                          description="work", branch="main"))
    q.dequeue(role="implementer")
    q.record_attribution(task_id="ff", agent="codex", role="implementer",
                         task_type="implement", status="in_progress")

    assert q.force_fail("ff", "watchdog timeout",
                        error_info={"reason": "watchdog_timeout"}) == "implement"

    attr = q.get_attribution("ff")
    assert attr["status"] == "failed"
    assert attr["outcome"] == "failed:watchdog_timeout"
    assert attr["completed_at"]
