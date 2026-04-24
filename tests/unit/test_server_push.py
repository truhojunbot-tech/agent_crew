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


# U-SP13: _default_push uses tmux load-buffer + paste-buffer -p so multi-line
# task text is delivered as a single bracketed paste (one Enter to submit).
# Before this fix, send-keys -l turned each \n in the task block into a
# separate Enter keystroke, which codex would submit prematurely and then hit
# its upstream rate limit.
def test_u_sp13_default_push_uses_bracketed_paste(monkeypatch):
    from unittest.mock import MagicMock
    from agent_crew.server import _default_push

    # capture-pane returns empty (task marker absent) → no retry Enter needed.
    mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout=""))
    monkeypatch.setattr("agent_crew.server.subprocess.run", mock_run)
    monkeypatch.setattr("agent_crew.server.time.sleep", lambda _: None)

    text = "line1\nline2\nline3"
    _default_push("%42", text)

    # 4 subprocess calls: load-buffer, paste-buffer -p -d, send-keys Enter,
    # capture-pane (verify Enter was processed). No retry because marker absent.
    assert mock_run.call_count == 4
    load_args = mock_run.call_args_list[0][0][0]
    paste_args = mock_run.call_args_list[1][0][0]
    enter_args = mock_run.call_args_list[2][0][0]
    capture_args = mock_run.call_args_list[3][0][0]

    assert load_args[:3] == ["tmux", "load-buffer", "-"]
    load_kwargs = mock_run.call_args_list[0][1]
    assert load_kwargs.get("input") == text

    assert "paste-buffer" in paste_args
    assert "-p" in paste_args  # bracketed paste mode
    assert "-d" in paste_args  # delete buffer after paste
    assert "%42" in paste_args

    assert enter_args == ["tmux", "send-keys", "-t", "%42", "Enter"]
    assert "capture-pane" in capture_args


# U-SP14: impl task completed → auto-enqueue review task
def test_u_sp14_impl_completed_auto_enqueue_review(tmp_db):
    push = RecordingPush()
    app = create_app(
        db_path=tmp_db,
        pane_map={"implementer": "%100", "reviewer": "%200"},
        port=8100,
        push_fn=push,
    )
    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("impl-001", "implement", "Add feature X"))
        assert len(push.calls) == 1
        assert "impl-001" in push.calls[0][1]

        resp = client.post("/tasks/impl-001/result", json=_result_payload("impl-001"))
        assert resp.status_code == 200

    assert len(push.calls) == 2
    review_push = push.calls[1]
    assert review_push[0] == "%200"  # review pane
    assert "review" in review_push[1]
    assert "impl-001" in review_push[1]  # prev_task_id in description or context


# U-SP15: impl task failed → no auto review enqueue
def test_u_sp15_impl_failed_no_auto_review(tmp_db):
    push = RecordingPush()
    app = create_app(
        db_path=tmp_db,
        pane_map={"implementer": "%100", "reviewer": "%200"},
        port=8100,
        push_fn=push,
    )
    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("impl-002", "implement"))
        assert len(push.calls) == 1

        resp = client.post(
            "/tasks/impl-002/result",
            json=_result_payload("impl-002", status="failed")
        )
        assert resp.status_code == 200

    assert len(push.calls) == 1  # no review task pushed


# U-SP16: impl task needs_human → no auto review enqueue
def test_u_sp16_impl_needs_human_no_auto_review(tmp_db):
    push = RecordingPush()
    app = create_app(
        db_path=tmp_db,
        pane_map={"implementer": "%100", "reviewer": "%200"},
        port=8100,
        push_fn=push,
    )
    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("impl-003", "implement"))
        assert len(push.calls) == 1

        resp = client.post(
            "/tasks/impl-003/result",
            json=_result_payload("impl-003", status="needs_human")
        )
        assert resp.status_code == 200

    assert len(push.calls) == 1  # no review task pushed


# U-SP17: auto-enqueued review task can be queried via /tasks (it gets pushed, so check in_progress)
def test_u_sp17_auto_review_task_queryable(tmp_db):
    push = RecordingPush()
    app = create_app(
        db_path=tmp_db,
        pane_map={"implementer": "%100", "reviewer": "%200"},
        port=8100,
        push_fn=push,
    )
    with TestClient(app) as client:
        client.post("/tasks", json=_task_payload("impl-004", "implement", "Fix bug Y"))
        client.post("/tasks/impl-004/result", json=_result_payload("impl-004"))

        # Auto-enqueued review task gets pushed immediately, so it's in_progress
        tasks = client.get("/tasks").json()
        review_tasks = [t for t in tasks if t["task_type"] == "review"]
        assert len(review_tasks) >= 1
        review = review_tasks[0]
        assert "impl-004" in str(review.get("context", {}))
        assert "Fix bug Y" in review["description"]
