"""#330 — review-comment operations must use the reviewed repository identity."""
import inspect
import json

import pytest
from fastapi.testclient import TestClient

from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


REVIEWER_WORKTREE = "/fake/quota-ops-worktree"
REVIEWER_REPO = "truhojunbot-tech/quota-ops"


@pytest.fixture(autouse=True)
def _live_test_panes(monkeypatch):
    """Keep repository tests on the worker result path."""
    monkeypatch.setattr("agent_crew.server._resolve_tmux_pane_target", lambda target: target)
    monkeypatch.setattr("agent_crew.server._pane_alive_for_push", lambda pane: True)
    monkeypatch.setattr("agent_crew.server._pane_process_kind", lambda pane: ("agent", "test"))


def _state_path(tmp_path, roles):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"roles": roles}))
    return str(path)


def _app(tmp_db, state_path):
    panes = {"reviewer": "%X", "codex": "%X"}
    return create_app(
        db_path=tmp_db,
        pane_map=panes,
        port=8201,
        push_fn=lambda *args, **kwargs: None,
        watchdog_disabled=True,
        state_path=state_path,
    )


def _review_payload(task_id, pr_number, context=None):
    return {
        "task_id": task_id,
        "task_type": "review",
        "description": "Review PR",
        "branch": "agent/agent_crew/codex",
        "priority": 3,
        "context": {"pr_number": pr_number, **(context or {})},
        "project": "",
    }


def _result_payload(task_id, pr_number, verdict="approve", findings=None):
    return {
        "task_id": task_id,
        "status": "completed",
        "summary": "looks good",
        "verdict": verdict,
        "findings": findings if findings is not None else [],
        "pr_number": pr_number,
    }


def _reviewer_repo(cwd=None):
    if cwd == REVIEWER_WORKTREE:
        return REVIEWER_REPO
    if cwd is None:
        return "truhojunbot-tech/alfred"
    return None


def _submit(app, task_id, pr_number, context=None, verdict="approve", findings=None):
    with TestClient(app) as client:
        created = client.post("/tasks", json=_review_payload(task_id, pr_number, context))
        assert created.status_code == 201, created.text
        response = client.post(
            f"/tasks/{task_id}/result",
            json=_result_payload(task_id, pr_number, verdict, findings),
        )
        assert response.status_code == 200, response.text
        return response


def test_wrong_cwd_never_used_all_three_calls_use_reviewer_repo(tmp_db, tmp_path, monkeypatch):
    calls = []
    state_path = _state_path(tmp_path, [{"role": "reviewer", "worktree": REVIEWER_WORKTREE}])
    monkeypatch.setattr("agent_crew.server.get_repo", _reviewer_repo, raising=False)
    monkeypatch.setattr("agent_crew.github.post_review_comment",
                        lambda **kwargs: calls.append(kwargs) or True)

    _submit(_app(tmp_db, state_path), "review-330-wrong-cwd", 330)

    assert calls[0]["repo"] == REVIEWER_REPO
    assert calls[0]["repo"] != "truhojunbot-tech/alfred"


def test_posting_and_reconciliation_use_byte_for_byte_identical_repo(tmp_db, tmp_path, monkeypatch):
    posts, checks = [], []
    state_path = _state_path(tmp_path, [{"role": "reviewer", "worktree": REVIEWER_WORKTREE}])
    monkeypatch.setattr("agent_crew.server.get_repo", _reviewer_repo, raising=False)
    monkeypatch.setattr("agent_crew.github.post_review_comment",
                        lambda **kwargs: posts.append(kwargs) or True)
    monkeypatch.setattr("agent_crew.github.pr_has_comment_containing",
                        lambda *args, **kwargs: checks.append(kwargs) or False)
    app = _app(tmp_db, state_path)
    task_id, pr_number = "review-330-reconcile", 331

    with TestClient(app) as client:
        assert client.post("/tasks", json=_review_payload(task_id, pr_number)).status_code == 201
        TaskQueue(tmp_db).external_op_reserve(f"comment:review:{task_id}", pr_number=pr_number)
        response = client.post(f"/tasks/{task_id}/result", json=_result_payload(task_id, pr_number))
        assert response.status_code == 200, response.text

    assert checks[0]["repo"] == posts[0]["repo"] == REVIEWER_REPO


def test_missing_repo_identity_is_fail_closed_zero_mutation(tmp_db, tmp_path, monkeypatch):
    state_path = _state_path(tmp_path, [])
    monkeypatch.setattr("agent_crew.server.get_repo", lambda cwd=None: None, raising=False)
    monkeypatch.setattr("agent_crew.github.post_review_comment",
                        lambda **kwargs: pytest.fail("post must not be attempted"))
    monkeypatch.setattr("agent_crew.github.pr_has_comment_containing",
                        lambda *args, **kwargs: pytest.fail("reconciliation must not be attempted"))

    response = _submit(_app(tmp_db, state_path), "review-330-no-repo", 332)

    assert response.json()["status"] == "ok"


def test_remote_success_local_crash_no_duplicate_comment(tmp_db, tmp_path, monkeypatch):
    posts = []
    state_path = _state_path(tmp_path, [{"role": "reviewer", "worktree": REVIEWER_WORKTREE}])
    monkeypatch.setattr("agent_crew.server.get_repo", _reviewer_repo, raising=False)
    monkeypatch.setattr("agent_crew.github.post_review_comment",
                        lambda **kwargs: posts.append(kwargs) or True)
    monkeypatch.setattr("agent_crew.github.pr_has_comment_containing", lambda *args, **kwargs: True)
    app = _app(tmp_db, state_path)
    task_id, pr_number = "review-330-already-posted", 333

    with TestClient(app) as client:
        assert client.post("/tasks", json=_review_payload(task_id, pr_number)).status_code == 201
        TaskQueue(tmp_db).external_op_reserve(f"comment:review:{task_id}", pr_number=pr_number)
        response = client.post(f"/tasks/{task_id}/result", json=_result_payload(task_id, pr_number))
        assert response.status_code == 200, response.text

    assert posts == []


def test_stop_race_admission_gate_unchanged(tmp_db, tmp_path, monkeypatch):
    posts = []
    state_path = _state_path(tmp_path, [{"role": "reviewer", "worktree": REVIEWER_WORKTREE}])
    monkeypatch.setattr("agent_crew.server.get_repo", _reviewer_repo, raising=False)
    monkeypatch.setattr("agent_crew.github.post_review_comment",
                        lambda **kwargs: posts.append(kwargs) or True)
    app = _app(tmp_db, state_path)
    task_id, pr_number = "review-330-stop", 334

    with TestClient(app) as client:
        assert client.post("/tasks", json=_review_payload(task_id, pr_number)).status_code == 201
        queue = TaskQueue(tmp_db)
        queue.set_stop_epoch(True, incident="issue-330-test")
        response = client.post(f"/tasks/{task_id}/result", json=_result_payload(task_id, pr_number))
        assert response.status_code == 200, response.text

    receipt = TaskQueue(tmp_db).external_op_get(f"comment:review:{task_id}")
    assert receipt is None
    assert posts == []


def test_coordinator_managed_cascade_suppression_unchanged(tmp_db, tmp_path, monkeypatch):
    state_path = _state_path(tmp_path, [{"role": "reviewer", "worktree": REVIEWER_WORKTREE}])
    monkeypatch.setattr("agent_crew.server.get_repo", _reviewer_repo, raising=False)
    monkeypatch.setattr("agent_crew.github.post_review_comment", lambda **kwargs: True)

    _submit(
        _app(tmp_db, state_path), "review-330-coordinator", 335,
        context={"coordinator_managed": True}, verdict="request_changes", findings=["fix this"],
    )

    assert not [task for task in TaskQueue(tmp_db).list_tasks() if task.task_type == "implement"]


def test_clean_checkout_no_bare_get_repo_call_in_review_result_path():
    from agent_crew import server

    source = inspect.getsource(server)
    start = source.index('            if task_type == "review":', source.index("def submit_result"))
    end = source.index("            # #304 review", start)
    review_result_path = source[start:end]
    assert "get_repo()" not in review_result_path
