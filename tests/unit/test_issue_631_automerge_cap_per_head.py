"""The merge retry cap belongs to the PR head that incurred the failures (#631)."""

import logging

import pytest
from fastapi.testclient import TestClient

from agent_crew import github
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


@pytest.mark.parametrize("opt_out", [True, False])
def test_new_head_runs_after_three_failures_while_same_head_stays_capped(
        tmp_path, monkeypatch, caplog, opt_out):
    if opt_out:
        monkeypatch.setenv("AGENT_CREW_AUTO_MERGE", "0")
    else:
        monkeypatch.delenv("AGENT_CREW_AUTO_MERGE", raising=False)
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(task_id="impl-631", task_type="implement",
                              description="work", branch="fix/631",
                              context={"issue": 631}))
    queue.submit_result("impl-631", TaskResult("impl-631", "completed", "done"))
    def add_review(suffix, sha):
        review_id = f"review-631-{suffix}"
        queue.enqueue(TaskRequest(
            task_id=review_id, task_type="review", description="review",
            branch="fix/631", context={"prev_task_id": "impl-631",
                "repo": "owner/repo", "pr_number": 631,
                "reviewed_sha": sha, "allow_duplicate_review": True}))
        queue.submit_result(review_id, TaskResult(
            review_id, "completed", "approved", verdict="approve", pr_number=631))

    add_review("a", "a" * 40)

    head = ["a" * 40]
    approved = [False]
    calls = []
    monkeypatch.setattr(github, "pr_state", lambda *a, **k: "open")
    monkeypatch.setattr(github, "pr_head_sha", lambda *a, **k: head[0])
    monkeypatch.setattr("agent_crew.conformance_gate._conformance_gate_allows_merge",
                        lambda *a, **k: True)

    def review_for_head(*args):
        if not approved[0]:
            return "", "", "", "review check unavailable"
        return head[0], "claude", f"review-631-{'a' if head[0][0] == 'a' else 'b'}", "ok"

    monkeypatch.setattr(github, "independent_review_for_head", review_for_head)
    monkeypatch.setattr(github, "publish_independent_review_status",
                        lambda *a, **k: calls.append(("status", a[1])) or True)
    monkeypatch.setattr(github, "independent_review_succeeded", lambda *a, **k: True)
    monkeypatch.setattr(github, "merge_pr",
                        lambda *a, **k: calls.append(("merge", head[0])) or True)
    app = create_app(str(tmp_path / "tasks.db"), pane_map={}, project="agent_crew",
                     push_fn=lambda *a, **k: None,
                     watchdog_disabled=True, anomaly_disabled=True)

    def post_test(client, task_id, review_id):
        queue.enqueue(TaskRequest(
            task_id=task_id, task_type="test", description="test", branch="fix/631",
            context={"prev_task_id": review_id, "repo": "owner/repo",
                     "pr_number": 631, "allow_duplicate_review": True}))
        response = client.post(f"/tasks/{task_id}/result", json={
            "task_id": task_id, "status": "completed", "summary": "passed",
            "pr_number": 631})
        assert response.status_code == 200, response.text

    with caplog.at_level(logging.INFO), TestClient(app) as client:
        for index in range(3):
            post_test(client, f"test-631-a{index}", "review-631-a")
        op = queue.external_op_get("merge:pr:631")
        assert op["state"] == "failed" and op["attempt"] == 3
        approved[0] = True
        post_test(client, "test-631-a-capped", "review-631-a")
        assert calls == []
        assert queue.external_op_get("merge:pr:631")["attempt"] == 3

        head[0] = "b" * 40
        add_review("b", "b" * 40)
        post_test(client, "test-631-b", "review-631-b")

    expected = [("status", "b" * 40)]
    if not opt_out:
        expected.append(("merge", "b" * 40))
    assert calls == expected
    op = queue.external_op_get("merge:pr:631")
    assert op["state"] == ("skipped" if opt_out else "done")
    assert op["attempt"] == 0
    if opt_out:
        assert "PR #631 merge left to coordinator" in caplog.text
