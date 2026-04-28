"""AGENT_CREW_DELIVERY env-flag gating (Issue #119 phase 6a).

Phase 6a of the cutover: introduce a single knob that lets the operator
turn off the tmux push path without removing the code. With ``mcp`` set,
the server stops pushing and agents must rely on the MCP pull loop —
which gives us a way to validate the cutover in production before phase
6b/6c remove the push code entirely.

Accepted values:
    "push"  → push enabled (legacy behavior)
    "mcp"   → push disabled
    "both"  → push enabled (default; alias for push since MCP pull is
              always available alongside)
    anything else → fall back to default (push enabled)

Tested at the HTTP boundary because that's the surface the operator
actually flips: a fresh ``crew setup`` reads the env when the server
spawns, so a pure unit test against ``create_app`` mirrors production.
"""
from fastapi.testclient import TestClient

from agent_crew.server import create_app


def _task_payload(task_id="t1", task_type="implement"):
    return {
        "task_id": task_id,
        "task_type": task_type,
        "description": "do work",
        "branch": "main",
        "priority": 3,
        "context": {},
        "project": "",
    }


class _RecordingPush:
    def __init__(self):
        self.calls = []

    def __call__(self, pane_id, text):
        self.calls.append((pane_id, text))


PANE_MAP = {"implementer": "%100", "claude": "%100"}


class TestDefaultIsBoth:
    def test_no_env_pushes_normally(self, tmp_db, monkeypatch):
        monkeypatch.delenv("AGENT_CREW_DELIVERY", raising=False)
        push = _RecordingPush()
        app = create_app(
            db_path=tmp_db, pane_map=PANE_MAP, port=8100, push_fn=push,
        )
        with TestClient(app) as client:
            client.post("/tasks", json=_task_payload("t1"))
        assert len(push.calls) == 1


class TestExplicitValues:
    def test_both_pushes(self, tmp_db, monkeypatch):
        monkeypatch.setenv("AGENT_CREW_DELIVERY", "both")
        push = _RecordingPush()
        app = create_app(
            db_path=tmp_db, pane_map=PANE_MAP, port=8100, push_fn=push,
        )
        with TestClient(app) as client:
            client.post("/tasks", json=_task_payload("t-both"))
        assert len(push.calls) == 1

    def test_push_pushes(self, tmp_db, monkeypatch):
        monkeypatch.setenv("AGENT_CREW_DELIVERY", "push")
        push = _RecordingPush()
        app = create_app(
            db_path=tmp_db, pane_map=PANE_MAP, port=8100, push_fn=push,
        )
        with TestClient(app) as client:
            client.post("/tasks", json=_task_payload("t-push"))
        assert len(push.calls) == 1

    def test_mcp_does_not_push(self, tmp_db, monkeypatch):
        monkeypatch.setenv("AGENT_CREW_DELIVERY", "mcp")
        push = _RecordingPush()
        app = create_app(
            db_path=tmp_db, pane_map=PANE_MAP, port=8100, push_fn=push,
        )
        with TestClient(app) as client:
            # Task is enqueued but no push fires.
            resp = client.post("/tasks", json=_task_payload("t-mcp"))
            assert resp.status_code == 201
        assert push.calls == []

    def test_unrecognized_value_falls_back_to_default(self, tmp_db, monkeypatch):
        monkeypatch.setenv("AGENT_CREW_DELIVERY", "bogus")
        push = _RecordingPush()
        app = create_app(
            db_path=tmp_db, pane_map=PANE_MAP, port=8100, push_fn=push,
        )
        with TestClient(app) as client:
            client.post("/tasks", json=_task_payload("t-bogus"))
        # An invalid value must not silently disable push — that would
        # be a config-typo footgun. Default = push enabled.
        assert len(push.calls) == 1


class TestMcpStillCascadesAfterSubmit:
    """With push off, the auto-enqueue cascade still runs (#123). The
    review task lands in the queue, just without a push notification —
    the agent's MCP loop will pick it up on the next get_next_task."""

    def test_impl_completed_creates_review_in_queue_without_push(
        self, tmp_db, monkeypatch
    ):
        monkeypatch.setenv("AGENT_CREW_DELIVERY", "mcp")
        push = _RecordingPush()
        app = create_app(
            db_path=tmp_db, pane_map=PANE_MAP, port=8100, push_fn=push,
        )
        with TestClient(app) as client:
            client.post("/tasks", json=_task_payload("imp-1"))
            client.post(
                "/tasks/imp-1/result",
                json={
                    "task_id": "imp-1",
                    "status": "completed",
                    "summary": "done",
                    "verdict": None,
                    "findings": [],
                    "pr_number": None,
                },
            )
            tasks = client.get("/tasks").json()
        # Review task got created.
        assert any(t["task_type"] == "review" for t in tasks)
        # No push fired anywhere along the way.
        assert push.calls == []
