"""Server-side integration tests for the rate-limit auto-fallback (Issue #81)."""
import json

from fastapi.testclient import TestClient

from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _task_payload(task_id="t1", task_type="implement", description="do work"):
    return {
        "task_id": task_id,
        "task_type": task_type,
        "description": description,
        "branch": "main",
        "priority": 3,
        "context": {},
        "project": "",
    }


def _result(task_id, status="failed", summary="rate limit reached", findings=None):
    return {
        "task_id": task_id,
        "status": status,
        "summary": summary,
        "verdict": None,
        "findings": findings or [],
        "pr_number": None,
    }


def _make_app(tmp_db, *, push_calls, panes=None, **kwargs):
    panes = panes or {
        "implementer": "%C", "claude": "%C",
        "reviewer": "%X",    "codex":  "%X",
        "tester": "%G",      "gemini": "%G",
    }

    def push(pane_id, text):
        push_calls.append((pane_id, text))

    return create_app(
        db_path=tmp_db,
        pane_map=panes,
        port=8200,
        push_fn=push,
        watchdog_disabled=True,
        **kwargs,
    )


# U-FB01: rate-limit hit on implementer (claude) reroutes the same task to codex.
def test_u_fb01_rate_limit_reroutes_implement_to_next_agent(tmp_db):
    push_calls: list = []
    app = _make_app(tmp_db, push_calls=push_calls)

    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("impl-1"))
        # Original push went to claude pane (%C).
        assert any(pane == "%C" for pane, _ in push_calls)

        # Implementer reports rate-limit failure.
        client.post("/tasks/impl-1/result", json=_result("impl-1"))

    # Fallback should have enqueued a new implement task (or it may be pushed already).
    rows = TaskQueue(tmp_db).list_all_with_status()
    fallback = [r for r in rows if r["task_id"].startswith("fallback-impl-1-")]
    assert len(fallback) == 1
    fallback_task = TaskQueue(tmp_db).list_tasks()
    fb_task = next(t for t in fallback_task if t.task_id.startswith("fallback-impl-1-"))
    assert fb_task.context["agent_override"] == "codex"
    assert fb_task.context["fallback_excluded"] == ["claude"]
    # The fallback push went to codex pane (%X).
    fallback_push = [c for c in push_calls if c[0] == "%X"]
    assert len(fallback_push) == 1


# U-FB02: rate-limit on the fallback agent (codex) cascades to gemini.
def test_u_fb02_rate_limit_chain_progresses(tmp_db):
    push_calls: list = []
    app = _make_app(tmp_db, push_calls=push_calls)

    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("impl-2"))
        client.post("/tasks/impl-2/result", json=_result("impl-2", summary="usage limit"))
        # Find the fallback task id and fail it too.
        fb_task = next(
            t for t in TaskQueue(tmp_db).list_tasks()
            if t.task_id.startswith("fallback-impl-2-")
        )
        client.post(f"/tasks/{fb_task.task_id}/result", json=_result(fb_task.task_id, summary="quota exceeded"))

    # Second fallback should target gemini, with both claude and codex excluded.
    cascading = [
        t for t in TaskQueue(tmp_db).list_tasks()
        if t.task_id.startswith("fallback-")
    ]
    # Two fallback tasks exist: claude→codex, then codex→gemini.
    assert len(cascading) == 2
    second = next(t for t in cascading if t.context.get("agent_override") == "gemini")
    assert sorted(second.context["fallback_excluded"]) == ["claude", "codex"]


# U-FB03: chain exhaustion creates an escalation gate; no further fallback enqueued.
def test_u_fb03_chain_exhaustion_creates_escalation_gate(tmp_db):
    push_calls: list = []
    notify_calls: list = []

    # Patch notify_telegram to capture the call without going to the network.
    import agent_crew.server as server_mod
    original_notify = server_mod.notify_telegram
    server_mod.notify_telegram = lambda msg, **kw: (notify_calls.append(msg), True)[1]
    try:
        app = _make_app(tmp_db, push_calls=push_calls)
        with TestClient(app) as client:
            client.post("/tasks", json=_task_payload("impl-3"))
            client.post("/tasks/impl-3/result", json=_result("impl-3", summary="rate limit"))
            fb1 = next(
                t for t in TaskQueue(tmp_db).list_tasks()
                if t.task_id.startswith("fallback-impl-3-")
            )
            client.post(f"/tasks/{fb1.task_id}/result", json=_result(fb1.task_id, summary="quota exceeded"))
            fb2 = next(
                t for t in TaskQueue(tmp_db).list_tasks()
                if t.task_id.startswith("fallback-") and t.task_id != fb1.task_id
            )
            client.post(f"/tasks/{fb2.task_id}/result", json=_result(fb2.task_id, summary="usage limit"))
    finally:
        server_mod.notify_telegram = original_notify

    # No 4th fallback enqueued.
    fallback_tasks = [
        t for t in TaskQueue(tmp_db).list_tasks()
        if t.task_id.startswith("fallback-")
    ]
    assert len(fallback_tasks) == 2
    # Escalation gate created.
    gates = TaskQueue(tmp_db).list_gates()
    escalations = [g for g in gates if g.type == "escalation"]
    assert len(escalations) == 1
    assert "impl-3" in escalations[0].message or "rate-limit" in escalations[0].message
    # Notify helper was called.
    assert notify_calls and "rate-limit" in notify_calls[0].lower()


# U-FB04: non-rate-limit failure falls through to the auto-retry path
# (not the fallback path) — fallback must NOT trigger.
def test_u_fb04_non_rate_limit_failure_skips_fallback(tmp_db):
    push_calls: list = []
    app = _make_app(tmp_db, push_calls=push_calls)

    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("impl-4"))
        client.post(
            "/tasks/impl-4/result",
            json=_result(
                "impl-4",
                summary="syntax error in foo.py",
                findings=["unexpected indent"],
            ),
        )

    # Fallback should NOT have enqueued a new task.
    fallback_tasks = [
        t for t in TaskQueue(tmp_db).list_tasks()
        if t.task_id.startswith("fallback-")
    ]
    assert fallback_tasks == []
    # But the existing auto-retry path should have produced a retry task.
    retry_tasks = [
        t for t in TaskQueue(tmp_db).list_tasks()
        if t.task_id.startswith("retry-")
    ]
    assert len(retry_tasks) == 1


# U-FB05: AGENT_CREW_FALLBACK_DISABLED forces the legacy retry path.
def test_u_fb05_disabled_via_env(tmp_db, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_FALLBACK_DISABLED", "1")
    push_calls: list = []
    app = _make_app(tmp_db, push_calls=push_calls)

    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("impl-5"))
        client.post("/tasks/impl-5/result", json=_result("impl-5", summary="rate limit"))

    # No fallback path was taken; the existing retry path was used instead.
    fallback_tasks = [
        t for t in TaskQueue(tmp_db).list_tasks()
        if t.task_id.startswith("fallback-")
    ]
    assert fallback_tasks == []
    retry_tasks = [
        t for t in TaskQueue(tmp_db).list_tasks()
        if t.task_id.startswith("retry-")
    ]
    assert len(retry_tasks) == 1


# U-FB06: per-project chain override (~/.agent_crew/<project>/fallback_chains.json)
# changes the next-agent decision.
def test_u_fb06_chain_override_respected(tmp_db, tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text("{}")
    override = tmp_path / "fallback_chains.json"
    override.write_text(json.dumps({"implement": ["claude", "gemini", "codex"]}))

    push_calls: list = []
    app = _make_app(tmp_db, push_calls=push_calls, state_path=str(state_path))

    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("impl-6"))
        client.post("/tasks/impl-6/result", json=_result("impl-6", summary="rate limit"))

    # Override chain: claude → gemini (not codex).
    fb_task = next(
        t for t in TaskQueue(tmp_db).list_tasks()
        if t.task_id.startswith("fallback-impl-6-")
    )
    assert fb_task.context["agent_override"] == "gemini"
