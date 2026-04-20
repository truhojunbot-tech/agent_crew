"""Unit tests for server push model (Option B: server pushes tasks to panes).

The server's contract:
- On POST /tasks: if the role's pane is registered and the role is idle (no
  in_progress task of that type), dequeue the task and push it to the pane.
- On POST /tasks/{id}/result: if the role has more pending tasks, push the
  next one to the same pane.
- push_fn is injectable so these tests don't actually run tmux.
"""
from fastapi.testclient import TestClient

from agent_crew.server import create_app


def _task_payload(task_id="t1", task_type="implement", description="do work", priority=3):
    return {
        "task_id": task_id,
        "task_type": task_type,
        "description": description,
        "branch": "main",
        "priority": priority,
        "context": {},
    }


def _result_payload(task_id, status="completed", summary="done"):
    return {
        "task_id": task_id,
        "status": status,
        "summary": summary,
        "verdict": None,
        "findings": [],
        "pr_number": None,
    }


class RecordingPush:
    def __init__(self):
        self.calls = []

    def __call__(self, pane_id, text):
        self.calls.append((pane_id, text))


# U-SP01: POST /tasks on idle role → push fires to the role's pane
def test_u_sp01_post_task_pushes_when_role_idle(tmp_db):
    push = RecordingPush()
    app = create_app(
        db_path=tmp_db,
        pane_map={"implementer": "%100"},
        port=9999,
        push_fn=push,
    )
    with TestClient(app) as client:
        resp = client.post("/tasks", json=_task_payload("t1", "implement"))
        assert resp.status_code == 201

    assert len(push.calls) == 1
    pane_id, text = push.calls[0]
    assert pane_id == "%100"
    assert "t1" in text
    assert "implement" in text
    assert "9999" in text  # port embedded in result POST URL


# U-SP02: Second POST /tasks for the same role while first is in-progress → no push
def test_u_sp02_second_task_queues_when_role_busy(tmp_db):
    push = RecordingPush()
    app = create_app(
        db_path=tmp_db,
        pane_map={"implementer": "%100"},
        port=8100,
        push_fn=push,
    )
    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("t1", "implement"))
        # t1 now in_progress; second post should NOT push
        client.post("/tasks", json=_task_payload("t2", "implement", priority=1))

    assert len(push.calls) == 1
    assert "t1" in push.calls[0][1]


# U-SP03: POST /result → next pending task is pushed automatically
def test_u_sp03_result_submission_triggers_next_push(tmp_db):
    push = RecordingPush()
    app = create_app(
        db_path=tmp_db,
        pane_map={"implementer": "%100"},
        port=8100,
        push_fn=push,
    )
    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("t1", "implement"))
        client.post("/tasks", json=_task_payload("t2", "implement"))
        assert len(push.calls) == 1  # only t1 pushed so far

        resp = client.post("/tasks/t1/result", json=_result_payload("t1"))
        assert resp.status_code == 200

    assert len(push.calls) == 2
    assert "t2" in push.calls[1][1]


# U-SP04: No pane_map configured → no push, task stays pending
def test_u_sp04_no_pane_map_no_push(tmp_db):
    push = RecordingPush()
    app = create_app(
        db_path=tmp_db,
        pane_map=None,
        port=8100,
        push_fn=push,
    )
    with TestClient(app) as client:
        resp = client.post("/tasks", json=_task_payload("t1", "implement"))
        assert resp.status_code == 201

    assert push.calls == []


# U-SP05: Role not in pane_map → no push for that role
def test_u_sp05_role_without_pane_no_push(tmp_db):
    push = RecordingPush()
    app = create_app(
        db_path=tmp_db,
        pane_map={"implementer": "%100"},  # reviewer has no pane
        port=8100,
        push_fn=push,
    )
    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("r1", "review"))

    assert push.calls == []


# U-SP06: Different roles are independent — implement push doesn't block review push
def test_u_sp06_different_roles_push_independently(tmp_db):
    push = RecordingPush()
    app = create_app(
        db_path=tmp_db,
        pane_map={"implementer": "%100", "reviewer": "%200"},
        port=8100,
        push_fn=push,
    )
    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("t1", "implement"))
        client.post("/tasks", json=_task_payload("r1", "review"))

    assert len(push.calls) == 2
    pane_ids = {c[0] for c in push.calls}
    assert pane_ids == {"%100", "%200"}


def _discuss_payload(task_id, agent, topic="some topic", priority=3):
    return {
        "task_id": task_id,
        "task_type": "discuss",
        "description": f"Discuss: {topic}",
        "branch": "main",
        "priority": priority,
        "context": {"agent": agent, "round": 1},
    }


# U-SP07: discuss task routes to the pane keyed by context.agent (Bug #2 fix)
def test_u_sp07_discuss_pushes_to_agent_specific_pane(tmp_db):
    push = RecordingPush()
    app = create_app(
        db_path=tmp_db,
        pane_map={
            "implementer": "%100", "reviewer": "%200", "tester": "%300",
            "claude": "%100", "codex": "%200", "gemini": "%300",
        },
        port=9000,
        push_fn=push,
    )
    with TestClient(app) as client:
        client.post("/tasks", json=_discuss_payload("d-claude", "claude"))

    assert len(push.calls) == 1
    pane_id, text = push.calls[0]
    assert pane_id == "%100"
    assert "d-claude" in text
    assert "discuss" in text


# U-SP08: three concurrent discuss tasks fan out to three panes simultaneously
def test_u_sp08_discuss_fans_out_across_panels(tmp_db):
    push = RecordingPush()
    app = create_app(
        db_path=tmp_db,
        pane_map={
            "claude": "%100", "codex": "%200", "gemini": "%300",
        },
        port=9000,
        push_fn=push,
    )
    with TestClient(app) as client:
        client.post("/tasks", json=_discuss_payload("d-claude", "claude"))
        client.post("/tasks", json=_discuss_payload("d-codex", "codex"))
        client.post("/tasks", json=_discuss_payload("d-gemini", "gemini"))

    assert len(push.calls) == 3
    routed = {c[0]: c[1] for c in push.calls}
    assert "d-claude" in routed["%100"]
    assert "d-codex" in routed["%200"]
    assert "d-gemini" in routed["%300"]


# U-SP09: two discuss tasks for the SAME agent → first pushes, second waits
def test_u_sp09_same_agent_discuss_serializes(tmp_db):
    push = RecordingPush()
    app = create_app(
        db_path=tmp_db,
        pane_map={"claude": "%100", "codex": "%200"},
        port=9000,
        push_fn=push,
    )
    with TestClient(app) as client:
        client.post("/tasks", json=_discuss_payload("d-claude-1", "claude"))
        client.post("/tasks", json=_discuss_payload("d-claude-2", "claude", priority=1))

    assert len(push.calls) == 1
    assert "d-claude-1" in push.calls[0][1]


# U-SP10: result submission for a discuss task triggers the next discuss for the same agent
def test_u_sp10_discuss_result_triggers_next_for_same_agent(tmp_db):
    push = RecordingPush()
    app = create_app(
        db_path=tmp_db,
        pane_map={"claude": "%100"},
        port=9000,
        push_fn=push,
    )
    with TestClient(app) as client:
        client.post("/tasks", json=_discuss_payload("d-claude-1", "claude"))
        client.post("/tasks", json=_discuss_payload("d-claude-2", "claude"))
        assert len(push.calls) == 1  # only first pushed

        resp = client.post(
            "/tasks/d-claude-1/result",
            json=_result_payload("d-claude-1"),
        )
        assert resp.status_code == 200

    assert len(push.calls) == 2
    assert "d-claude-2" in push.calls[1][1]
    assert push.calls[1][0] == "%100"


# U-SP11: discuss task for an agent not in pane_map → no push (graceful skip)
def test_u_sp11_discuss_unknown_agent_no_push(tmp_db):
    push = RecordingPush()
    app = create_app(
        db_path=tmp_db,
        pane_map={"claude": "%100"},
        port=9000,
        push_fn=push,
    )
    with TestClient(app) as client:
        resp = client.post(
            "/tasks", json=_discuss_payload("d-unknown", "rando-agent"),
        )
        assert resp.status_code == 201

    assert push.calls == []


# U-SP12: discuss task without context.agent → no push (malformed, graceful)
def test_u_sp12_discuss_missing_agent_no_push(tmp_db):
    push = RecordingPush()
    app = create_app(
        db_path=tmp_db,
        pane_map={"claude": "%100"},
        port=9000,
        push_fn=push,
    )
    payload = _discuss_payload("d-noagent", "claude")
    payload["context"] = {}  # no agent key
    with TestClient(app) as client:
        resp = client.post("/tasks", json=payload)
        assert resp.status_code == 201

    assert push.calls == []
