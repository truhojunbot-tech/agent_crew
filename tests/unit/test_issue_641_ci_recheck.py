"""The dispatcher revisits a pending merge without losing its repo or slots."""

import threading
import time

from fastapi.testclient import TestClient

from agent_crew import github
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _pending_merge(tmp_path, monkeypatch, *, timeout=False):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", "0.02")
    monkeypatch.delenv("AGENT_CREW_AUTO_MERGE", raising=False)
    monkeypatch.setattr("agent_crew.server._pipeline_reresolve_pending_rounds_caps",
                        lambda *a, **k: None)
    monkeypatch.setattr("agent_crew.conformance_gate._conformance_gate_allows_merge",
                        lambda *a, **k: True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /fake")
    repo_cwds = []

    def get_repo(cwd=None):
        repo_cwds.append(cwd)
        return "owner/repo" if cwd == str(worktree) else None

    monkeypatch.setattr(github, "get_repo", get_repo)
    monkeypatch.setattr(github, "pr_head_sha", lambda *a, **k: "a" * 40)
    monkeypatch.setattr(github, "pr_state", lambda *a, **k: "open")
    monkeypatch.setattr(github, "independent_review_for_head",
                        lambda *a, **k: ("a" * 40, "claude", "review-641", "ok"))
    monkeypatch.setattr(github, "publish_independent_review_status", lambda *a, **k: True)
    monkeypatch.setattr(github, "independent_review_succeeded", lambda *a, **k: True)
    ci_calls = []

    def checks(*args):
        ci_calls.append(args)
        return ("pending", "unit") if len(ci_calls) == 1 or timeout else ("green", "")

    monkeypatch.setattr(github, "head_checks_state", checks)
    merged = threading.Event()
    monkeypatch.setattr(github, "merge_pr", lambda *a, **k: merged.set() or True)
    db = str(tmp_path / "tasks.db")
    queue = TaskQueue(db)
    queue.enqueue(TaskRequest("impl-641", "implement", "work", branch="fix/641"))
    queue.submit_result("impl-641", TaskResult("impl-641", "completed", "done"))
    queue.enqueue(TaskRequest("review-641", "review", "review", branch="fix/641",
                              context={"prev_task_id": "impl-641", "pr_number": 641}))
    queue.submit_result("review-641", TaskResult("review-641", "completed", "approved",
                                                  verdict="approve", pr_number=641))
    queue.enqueue(TaskRequest("test-641", "test", "test", branch="fix/641",
                              context={"prev_task_id": "review-641", "pr_number": 641}))
    # This fixture drives result submissions; leave task claims to dedicated
    # dispatcher tests so a pending follow-up is not failed for lack of panes.
    monkeypatch.setattr(TaskQueue, "dequeue", lambda self, *a, **k: None)
    monkeypatch.setattr(TaskQueue, "dequeue_discuss_for_agent",
                        lambda self, *a, **k: None)
    app = create_app(db, pane_map={}, project="agent_crew",
                     worktree_map={"implementer": str(worktree)},
                     push_fn=lambda *a, **k: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    return app, queue, repo_cwds, ci_calls, merged, str(worktree)


def _post_result(client, task_id):
    response = client.post(f"/tasks/{task_id}/result", json={
        "task_id": task_id, "status": "completed", "summary": "passed",
        "pr_number": 641})
    assert response.status_code == 200, response.text


def test_tick_rechecks_pending_then_merges_with_worktree_repo(tmp_path, monkeypatch):
    app, queue, repo_cwds, ci_calls, merged, worktree = _pending_merge(tmp_path, monkeypatch)
    with TestClient(app) as client:
        _post_result(client, "test-641")
        assert queue.external_op_get("merge:pr:641")["state"] == "reserved"
        time.sleep(0.1)
        assert len(ci_calls) == 1  # ten-second throttle prevents duplicate checks
        op = queue.external_op_get("merge:pr:641")
        since = op["last_error"].split("since=")[1].split()[0]
        queue.external_op_mark("merge:pr:641", "reserved", last_error=(
            f"head={'a' * 40} CI pending since={since} checked=0: unit"))
        assert merged.wait(2), "dispatcher tick did not merge after CI turned green"
    assert len(ci_calls) == 2
    assert repo_cwds == [worktree, worktree]
    assert queue.external_op_get("merge:pr:641")["state"] == "done"


def test_pending_timeout_preserves_since_and_stays_skipped(tmp_path, monkeypatch):
    app, queue, _, ci_calls, merged, _ = _pending_merge(tmp_path, monkeypatch, timeout=True)
    with TestClient(app) as client:
        _post_result(client, "test-641")
        op = queue.external_op_get("merge:pr:641")
        since = op["last_error"].split("since=")[1].split()[0]
        queue.external_op_mark("merge:pr:641", "reserved", last_error=(
            f"head={'a' * 40} CI pending since={since} checked=0: unit"))
        deadline = time.monotonic() + 2
        while len(ci_calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(ci_calls) == 2
        assert f"since={since}" in queue.external_op_get("merge:pr:641")["last_error"]
        queue.external_op_mark("merge:pr:641", "reserved", last_error=(
            f"head={'a' * 40} CI pending since={time.time() - 121} checked=0: unit"))
        deadline = time.monotonic() + 2
        while queue.external_op_get("merge:pr:641")["state"] != "skipped" and time.monotonic() < deadline:
            time.sleep(0.02)
        op = queue.external_op_get("merge:pr:641")
        assert op["state"] == "skipped"
        assert "CI pending timed out" in op["last_error"]
        queue.enqueue(TaskRequest("test-641-again", "test", "test", branch="fix/641",
                                  context={"prev_task_id": "review-641", "pr_number": 641,
                                           "allow_duplicate_review": True}))
        _post_result(client, "test-641-again")
    assert not merged.is_set()
    assert len(ci_calls) == 3


def test_ci_scan_failure_does_not_skip_dispatch_tick(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", "0.02")
    monkeypatch.setattr(TaskQueue, "pending_ci_merge_ops",
                        lambda self: (_ for _ in ()).throw(RuntimeError("scan failed")))
    monkeypatch.setattr("agent_crew.server._pipeline_reresolve_pending_rounds_caps",
                        lambda *a, **k: None)
    dispatched = threading.Event()

    def dequeue(self, *args, **kwargs):
        dispatched.set()
        return None

    monkeypatch.setattr(TaskQueue, "dequeue", dequeue)
    app = create_app(str(tmp_path / "tasks.db"), pane_map={}, project="agent_crew",
                     push_fn=lambda *a, **k: None,
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        assert dispatched.wait(1), "CI scan exception prevented task dispatch"
