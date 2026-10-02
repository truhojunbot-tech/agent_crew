"""Server-side integration tests for the rate-limit auto-fallback (Issue #81)."""
import json

import pytest

from fastapi.testclient import TestClient

from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


@pytest.fixture(autouse=True)
def _live_test_panes(monkeypatch):
    """Keep fallback tests on the worker-result path, not tmux refusal."""
    monkeypatch.setattr("agent_crew.server._resolve_tmux_pane_target", lambda target: target)
    monkeypatch.setattr("agent_crew.server._pane_alive_for_push", lambda pane: True)
    monkeypatch.setattr("agent_crew.server._pane_process_kind", lambda pane: ("agent", "test"))


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


@pytest.mark.parametrize("summary, successor_prefix", [
    ("worker failed", "retry-receipt-parent-"),
    ("rate limit reached", "retry-receipt-parent-"),
])
def test_successor_cea_blocks_belong_to_own_receipt(tmp_db, summary, successor_prefix):
    app = _make_app(tmp_db, push_calls=[])
    with TestClient(app) as client:
        response = client.post("/tasks", json=_task_payload("receipt-parent"))
        assert response.status_code == 201
        queue = TaskQueue(tmp_db)
        parent = next(t for t in queue.list_tasks() if t.task_id == "receipt-parent")
        queue.patch_context(parent.task_id, {
            "lineage_marker": "keep",
            "cea_cascade": {"parent_only": True},
            "cea_future_gate": {
                "receipt_id": parent.context["cea_enqueue"]["receipt_id"],
            },
        })

        response = client.post("/tasks/receipt-parent/result",
                               json=_result("receipt-parent", summary=summary))
        assert response.status_code == 200

    tasks = queue.list_tasks()
    parent = next(t for t in tasks if t.task_id == "receipt-parent")
    child = next(t for t in tasks if t.task_id.startswith(successor_prefix))
    parent_receipt = parent.context["cea_enqueue"]["receipt_id"]
    child_receipt = child.context["cea_enqueue"]["receipt_id"]
    assert parent.context["cea_result"]["receipt_id"] == parent_receipt
    assert child_receipt != parent_receipt
    assert child.context["lineage_marker"] == "keep"
    assert child.context["original_task_id"] == parent.task_id
    assert "cea_future_gate" not in child.context
    assert "cea_result" not in child.context
    assert "parent_only" not in child.context["cea_cascade"]
    for key, block in child.context.items():
        if key.startswith("cea_") and "receipt_id" in block:
            assert block["receipt_id"] == child_receipt, key


# #308: a rate limit may retry the same role but cannot substitute a provider.
def test_u_fb01_rate_limit_never_substitutes_implementer(tmp_db):
    push_calls: list = []
    app = _make_app(tmp_db, push_calls=push_calls)

    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("impl-1"))
        # Original push went to claude pane (%C).
        assert any(pane == "%C" for pane, _ in push_calls)

        # Implementer reports rate-limit failure.
        client.post("/tasks/impl-1/result", json=_result("impl-1"))

    # Same-role retry is allowed; no alternate provider receives this role.
    rows = TaskQueue(tmp_db).list_all_with_status()
    fallback = [r for r in rows if r["task_id"].startswith("fallback-impl-1-")]
    assert fallback == []
    retries = [t for t in TaskQueue(tmp_db).list_tasks()
               if t.task_id.startswith("retry-impl-1-")]
    assert len(retries) == 1
    assert "agent_override" not in retries[0].context
    assert not any(pane == "%X" for pane, _ in push_calls)


# #308: repeated rate limits still cannot create a cross-provider chain.
def test_u_fb02_repeated_rate_limit_has_no_fallback_chain(tmp_db):
    push_calls: list = []
    app = _make_app(tmp_db, push_calls=push_calls)

    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("impl-2"))
        client.post("/tasks/impl-2/result", json=_result("impl-2", summary="usage limit"))
        retry_task = next(
            t for t in TaskQueue(tmp_db).list_tasks()
            if t.task_id.startswith("retry-impl-2-")
        )
        client.post(f"/tasks/{retry_task.task_id}/result",
                    json=_result(retry_task.task_id, summary="quota exceeded"))

    assert not any(t.task_id.startswith("fallback-")
                   for t in TaskQueue(tmp_db).list_tasks())


# #308: an exhausted provider cannot create a cross-provider escalation chain.
def test_u_fb03_rate_limit_does_not_create_fallback_escalation(tmp_db):
    push_calls: list = []
    app = _make_app(tmp_db, push_calls=push_calls)
    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("impl-3"))
        client.post("/tasks/impl-3/result", json=_result("impl-3", summary="rate limit"))

    fallback_tasks = [
        t for t in TaskQueue(tmp_db).list_tasks()
        if t.task_id.startswith("fallback-")
    ]
    assert fallback_tasks == []
    gates = TaskQueue(tmp_db).list_gates()
    escalations = [g for g in gates if g.type == "escalation"]
    assert escalations == []


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


# #308: legacy chain configuration cannot revive provider substitution.
def test_u_fb06_chain_override_cannot_substitute(tmp_db, tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text("{}")
    override = tmp_path / "fallback_chains.json"
    override.write_text(json.dumps({"implement": ["claude", "gemini", "codex"]}))

    push_calls: list = []
    app = _make_app(tmp_db, push_calls=push_calls, state_path=str(state_path))

    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("impl-6"))
        client.post("/tasks/impl-6/result", json=_result("impl-6", summary="rate limit"))

    assert not any(t.task_id.startswith("fallback-")
                   for t in TaskQueue(tmp_db).list_tasks())
