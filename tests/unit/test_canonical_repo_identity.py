"""Canonical repository identity regressions A--J.

Each test maps directly to the lettered acceptance scenario in the remediation
specification.  The focused unit checks below supplement the existing cascade
and STOP suites, which are run as part of this change's verification.
"""

import ast
import inspect
import json

import pytest

from agent_crew import pipeline
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


TARGET_REPO = "truhojunbot-tech/quota-ops"
WRONG_REPO = "truhojunbot-tech/alfred"
WORKTREE = "/canonical/quota-ops"


def _repo_from_worktree(cwd=None):
    return TARGET_REPO if cwd == WORKTREE else WRONG_REPO if cwd is None else None


def _state_path(tmp_path, roles):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"roles": roles}))
    return str(path)


def _app(tmp_db, state_path):
    from agent_crew.server import create_app

    return create_app(db_path=tmp_db, pane_map={"reviewer": "%R", "tester": "%T"},
                      state_path=state_path, push_fn=lambda *args: None,
                      watchdog_disabled=True, anomaly_disabled=True)


def _post_task(client, task_id, task_type, context, pr_number=91001):
    response = client.post("/tasks", json={
        "task_id": task_id, "task_type": task_type, "description": task_type,
        "branch": "fix/canonical", "priority": 3, "context": context, "project": "",
    })
    assert response.status_code == 201, response.text
    return _submit_result(client, task_id, task_type, pr_number)


def _submit_result(client, task_id, task_type, pr_number=91001):
    return client.post(f"/tasks/{task_id}/result", json={
        "task_id": task_id, "status": "completed", "summary": "ok",
        "verdict": "approve" if task_type == "review" else None,
        "findings": [], "pr_number": pr_number,
    })


def _patch_open_pr(monkeypatch):
    monkeypatch.setattr("agent_crew.github.pr_state", lambda *args, **kwargs: "open")
    monkeypatch.setattr("agent_crew.github.post_review_comment", lambda **kwargs: True)


def test_G_unresolved_repo_is_unknown_not_a_process_cwd_guess(monkeypatch):
    """G — no repository signal short-circuits before GitHub is consulted."""
    monkeypatch.setattr(
        "agent_crew.github.pr_state",
        lambda *args, **kwargs: pytest.fail("pr_state must not be called"),
    )

    assert pipeline.pr_is_actionable(91001) == (False, "unknown")


def test_A_merge_uses_worktree_repo_not_dispatcher_cwd(tmp_db, tmp_path, monkeypatch):
    """A — review/no-tester and completed-test merges use quota-ops, never Alfred."""
    from fastapi.testclient import TestClient

    calls, state_calls = [], []
    worktree = tmp_path / "quota-ops-worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /fake")
    worktree_path = str(worktree)
    state_path = _state_path(tmp_path, [{"role": "reviewer", "worktree": worktree_path}])
    def repo_from_actual_worktree(cwd=None):
        return TARGET_REPO if cwd == worktree_path else WRONG_REPO if cwd is None else None
    _patch_open_pr(monkeypatch)
    monkeypatch.setattr("agent_crew.github.pr_state",
                        lambda pr, **kwargs: state_calls.append(kwargs["repo"]) or "open")
    monkeypatch.setattr("agent_crew.server.get_repo", repo_from_actual_worktree)
    monkeypatch.setattr("agent_crew.github.get_repo", repo_from_actual_worktree)
    monkeypatch.setattr("agent_crew.github.merge_pr",
                        lambda pr, **kwargs: calls.append(kwargs["repo"]) or True)
    app = _app(tmp_db, state_path)
    with TestClient(app) as client:
        assert _post_task(client, "review-A", "review", {"pr_number": 91002, "no_tester": True}, 91002).status_code == 200
        assert _post_task(client, "test-A", "test", {"pr_number": 91003}, 91003).status_code == 200
    assert state_calls == calls == [TARGET_REPO, TARGET_REPO]


def test_B_missing_repo_marks_merge_failed_without_github_calls(tmp_db, tmp_path, monkeypatch):
    """B — unresolved identity performs no PR read or merge mutation."""
    from fastapi.testclient import TestClient

    state_path = _state_path(tmp_path, [])
    _patch_open_pr(monkeypatch)
    monkeypatch.setattr("agent_crew.server.get_repo", lambda cwd=None: None)
    monkeypatch.setattr("agent_crew.github.get_repo", lambda cwd=None: None)
    monkeypatch.setattr("agent_crew.github.pr_state", lambda *a, **k: pytest.fail("no state read"))
    monkeypatch.setattr("agent_crew.github.merge_pr", lambda *a, **k: pytest.fail("no merge"))
    with TestClient(_app(tmp_db, state_path)) as client:
        response = _post_task(client, "review-B", "review", {"pr_number": 91004, "no_tester": True}, 91004)
    assert response.status_code == 200
    receipt = TaskQueue(tmp_db).external_op_get("merge:pr:91004")
    assert receipt["state"] == "failed"
    assert "repo identity unresolved" in receipt["last_error"]


def test_C_repo_survives_implement_review_test_lineage(tmp_db, tmp_path, monkeypatch):
    """C — an implement-context repo is persisted into both successors."""
    from fastapi.testclient import TestClient

    _patch_open_pr(monkeypatch)
    app = _app(tmp_db, _state_path(tmp_path, []))
    with TestClient(app) as client:
        assert _post_task(client, "impl-C", "implement", {"pr_number": 91005, "repo": TARGET_REPO}, 91005).status_code == 200
        review = next(t for t in TaskQueue(tmp_db).list_tasks()
                      if t.task_id == "review-impl-C-r0")
        assert review.context["repo"] == TARGET_REPO
        assert _submit_result(client, review.task_id, "review", 91005).status_code == 200
    test = next(t for t in TaskQueue(tmp_db).list_tasks()
                if t.task_id == "test-review-impl-C-r0")
    assert test.context["repo"] == TARGET_REPO


def test_D_no_tester_merge_uses_explicit_review_repo(tmp_db, tmp_path, monkeypatch):
    """D — no-tester approval passes the review task's canonical repo to merge."""
    from fastapi.testclient import TestClient

    calls = []
    _patch_open_pr(monkeypatch)
    monkeypatch.setattr("agent_crew.github.merge_pr", lambda pr, **kw: calls.append(kw["repo"]) or True)
    with TestClient(_app(tmp_db, _state_path(tmp_path, []))) as client:
        assert _post_task(client, "review-D", "review", {"pr_number": 91006, "repo": TARGET_REPO, "no_tester": True}, 91006).status_code == 200
    assert calls == [TARGET_REPO]
    assert not [t for t in TaskQueue(tmp_db).list_tasks() if t.task_type == "test"]


def test_E_fix_budget_check_and_post_use_worktree_repo(tmp_db, tmp_path, monkeypatch):
    """E — reconciliation and the final budget notice use one worktree-resolved repo."""
    checks, posts = [], []
    queue = TaskQueue(tmp_db)
    monkeypatch.setattr("agent_crew.github.get_repo", _repo_from_worktree)
    monkeypatch.setattr("agent_crew.github.pr_has_comment_containing",
                        lambda *a, **kw: checks.append(kw["repo"]) or False)
    monkeypatch.setattr("agent_crew.github.post_pr_comment",
                        lambda *a, **kw: posts.append(kw["repo"]) or True)
    pipeline._announce_fix_budget_exhausted(pr_number=91007, review_task_id="review-E",
                                            max_rounds=1, findings=[], queue=queue,
                                            repo_cwd=WORKTREE)
    assert checks == posts == [TARGET_REPO]


def test_F_terminal_gate_reads_the_worktree_repo_not_dispatcher_cwd(tmp_db, monkeypatch):
    """F — a merged result from quota-ops stops the review cascade."""
    seen = []
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id="impl-F", task_type="implement", description="impl",
                              branch="fix/canonical", context={"pr_number": 91008}))
    monkeypatch.setattr("agent_crew.github.get_repo", _repo_from_worktree)
    def state(pr, **kwargs):
        seen.append(kwargs.get("repo"))
        return "merged" if kwargs.get("repo") == TARGET_REPO else "open"
    monkeypatch.setattr("agent_crew.github.pr_state", state)
    assert pipeline.auto_enqueue_review(queue, "impl-F", 91008, repo_cwd=WORKTREE) is None
    assert seen == [TARGET_REPO]


def test_H_stop_admission_remains_before_merge_even_with_repo_params(tmp_db, tmp_path, monkeypatch):
    """H — STOP prevents a valid-repo no-tester merge and creates no receipt."""
    from fastapi.testclient import TestClient

    _patch_open_pr(monkeypatch)
    monkeypatch.setattr("agent_crew.github.merge_pr", lambda *a, **k: pytest.fail("STOP must block merge"))
    app = _app(tmp_db, _state_path(tmp_path, []))
    with TestClient(app) as client:
        created = client.post("/tasks", json={
            "task_id": "review-H", "task_type": "review", "description": "review",
            "branch": "fix/canonical", "priority": 3,
            "context": {"pr_number": 91009, "repo": TARGET_REPO, "no_tester": True},
            "project": "",
        })
        assert created.status_code == 201
        TaskQueue(tmp_db).set_stop_epoch(True, incident="canonical-test")
        assert _submit_result(client, "review-H", "review", 91009).status_code == 200
    assert TaskQueue(tmp_db).external_op_get("merge:pr:91009") is None


def test_I_coordinator_managed_still_suppresses_no_tester_merge(tmp_db, tmp_path, monkeypatch):
    """I — coordinator-managed approval never enters the no-tester merge path."""
    from fastapi.testclient import TestClient

    _patch_open_pr(monkeypatch)
    monkeypatch.setattr("agent_crew.github.merge_pr", lambda *a, **k: pytest.fail("merge suppressed"))
    with TestClient(_app(tmp_db, _state_path(tmp_path, []))) as client:
        assert _post_task(client, "review-I", "review", {"pr_number": 91010, "repo": TARGET_REPO, "no_tester": True, "coordinator_managed": True}, 91010).status_code == 200
    assert TaskQueue(tmp_db).external_op_get("merge:pr:91010") is None


def test_J_no_bare_repo_lookup_or_unidentified_pr_state_calls():
    """J — structural integrity guard for the paths changed by this remediation."""
    from agent_crew import server

    for module in (pipeline, server):
        assert "get_repo()" not in inspect.getsource(module)

    for source in (inspect.getsource(pipeline), inspect.getsource(server)):
        for call in (node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)):
            name = call.func.id if isinstance(call.func, ast.Name) else ""
            if name in {"pr_state", "_pr_state"}:
                assert {keyword.arg for keyword in call.keywords} & {"repo", "cwd"}
