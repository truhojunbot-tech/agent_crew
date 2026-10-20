"""A completed implement without a reviewable PR must not spend a reviewer."""

from unittest.mock import patch

from agent_crew.pipeline import auto_enqueue_review
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


def _completed_impl(tmp_path, *, crew_run_branch=False, branch="agent/work"):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(
        task_id="impl-no-pr", task_type="implement", description="Implement #666",
        branch=branch, context={
            "repo": "owner/repo", "crew_run_branch": crew_run_branch,
            "implementer_agent": "codex", "reviewer_agent": "claude",
            "agent_override": "codex",
        },
    ))
    queue.submit_result("impl-no-pr", TaskResult(
        task_id="impl-no-pr", status="completed", summary="pushed",
        branch=branch, commit="a" * 40,
    ))
    return queue


def _reviews(queue):
    return [task for task in queue.list_tasks() if task.task_type == "review"]


def _pr_opening_retries(queue):
    return [task for task in queue.list_tasks()
            if task.task_type == "implement"
            and task.context.get("retry_reason") == "no_pr_for_review"]


def test_no_pr_without_cli_branch_sends_one_notice_and_no_review(tmp_path):
    queue = _completed_impl(tmp_path)
    messages = []
    with patch("agent_crew.github.pr_number_for_branch", return_value=None), patch(
        "agent_crew.notify.notify_telegram", side_effect=lambda message: messages.append(message) or True
    ):
        assert auto_enqueue_review(queue, "impl-no-pr") is None
        assert auto_enqueue_review(queue, "impl-no-pr") is None
    assert _reviews(queue) == []
    retries = _pr_opening_retries(queue)
    assert len(retries) == 1
    assert retries[0].branch == "agent/work"
    assert retries[0].context["retry_attempt"] == 1
    assert retries[0].context["prev_task_id"] == "impl-no-pr"
    assert retries[0].context["agent_override"] == "codex"
    assert "open a pr" in retries[0].description.lower()
    assert queue.get_task("impl-no-pr").context["review_blocked_reason"] == "no_pr_for_review"
    assert len(messages) == 1
    assert "no_pr_for_review" in messages[0]


def test_pr_opened_on_retry_resumes_review(tmp_path):
    queue = _completed_impl(tmp_path)
    with patch("agent_crew.github.pr_number_for_branch", side_effect=[None, 666]), patch(
        "agent_crew.notify.notify_telegram", return_value=True
    ):
        assert auto_enqueue_review(queue, "impl-no-pr") is None
        retry = _pr_opening_retries(queue)[0]
        queue.submit_result(retry.task_id, TaskResult(
            task_id=retry.task_id, status="completed", summary="opened PR #666",
            branch="agent/work", commit="b" * 40,
        ))
        review_id = auto_enqueue_review(
            queue, retry.task_id, pr_state_fn=lambda number: "open"
        )
    assert review_id is not None
    assert len(_pr_opening_retries(queue)) == 1
    assert _reviews(queue)[0].context["pr_number"] == 666


def test_no_pr_retry_cap_sends_one_terminal_notice(tmp_path):
    queue = _completed_impl(tmp_path)
    messages = []
    with patch("agent_crew.github.pr_number_for_branch", return_value=None), patch(
        "agent_crew.notify.notify_telegram", side_effect=lambda message: messages.append(message) or True
    ):
        auto_enqueue_review(queue, "impl-no-pr")
        first = _pr_opening_retries(queue)[0]
        queue.submit_result(first.task_id, TaskResult(
            task_id=first.task_id, status="completed", summary="still no PR",
            branch="agent/work", commit="b" * 40,
        ))
        auto_enqueue_review(queue, first.task_id)
        second = max(_pr_opening_retries(queue), key=lambda task: task.context["retry_attempt"])
        assert second.context["retry_attempt"] == 2
        queue.submit_result(second.task_id, TaskResult(
            task_id=second.task_id, status="completed", summary="still no PR",
            branch="agent/work", commit="c" * 40,
        ))
        auto_enqueue_review(queue, second.task_id)
        auto_enqueue_review(queue, second.task_id)
    assert len(_pr_opening_retries(queue)) == 2
    assert _reviews(queue) == []
    assert len(messages) == 2
    assert "kind=no_pr_for_review_exhausted" in messages[1]
    assert "retry cap exhausted" in messages[1]
    assert "branch=agent/work" in messages[1]


def test_existing_pr_number_is_carried_to_review(tmp_path):
    queue = _completed_impl(tmp_path)
    with patch("agent_crew.github.pr_number_for_branch", return_value=666) as lookup:
        review_id = auto_enqueue_review(queue, "impl-no-pr", pr_state_fn=lambda number: "open")
    lookup.assert_called_once_with("agent/work", repo="owner/repo", state="open")
    assert review_id is not None
    assert len(_reviews(queue)) == 1
    assert _reviews(queue)[0].context["pr_number"] == 666


def test_cli_branch_can_still_enqueue_before_pr_exists(tmp_path):
    queue = _completed_impl(tmp_path, crew_run_branch=True)
    with patch("agent_crew.github.pr_number_for_branch", return_value=None):
        review_id = auto_enqueue_review(queue, "impl-no-pr")
    assert review_id is not None
    assert len(_reviews(queue)) == 1


def test_legacy_task_without_crew_run_branch_keeps_review_cascade(tmp_path):
    queue = TaskQueue(str(tmp_path / "legacy.db"))
    queue.enqueue(TaskRequest(
        "impl-legacy", "implement", "legacy branch", branch="agent/work",
    ))
    queue.submit_result("impl-legacy", TaskResult(
        task_id="impl-legacy", status="completed", summary="pushed",
        branch="agent/work", commit="a" * 40,
    ))
    with patch("agent_crew.github.pr_number_for_branch") as lookup:
        review_id = auto_enqueue_review(queue, "impl-legacy")
    assert review_id is not None
    assert len(_reviews(queue)) == 1
    assert _pr_opening_retries(queue) == []
    lookup.assert_not_called()


def test_no_branch_and_no_pr_keeps_161_guard(tmp_path):
    queue = _completed_impl(tmp_path, branch="")
    with patch("agent_crew.github.pr_number_for_branch", return_value=None):
        assert auto_enqueue_review(queue, "impl-no-pr") is None
    assert _reviews(queue) == []


def test_http_result_pushes_pr_opening_retry_to_implementer(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from agent_crew.server import create_app

    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "0")
    pushed = []
    app = create_app(
        db_path=str(tmp_path / "http.db"),
        pane_map={"implementer": "%100", "reviewer": "%200"},
        push_fn=lambda pane, message: pushed.append((pane, message)),
        watchdog_disabled=True, anomaly_disabled=True,
    )
    with patch("agent_crew.github.pr_number_for_branch", return_value=None), patch(
        "agent_crew.notify.notify_telegram", return_value=True
    ), TestClient(app) as client:
        response = client.post("/tasks", json={
            "task_id": "impl-http-666", "task_type": "implement",
            "description": "Implement #666", "branch": "agent/work",
            "context": {"repo": "owner/repo", "risk_tier": 1,
                        "crew_run_branch": False},
        })
        assert response.status_code == 201
        result = client.post("/tasks/impl-http-666/result", json={
            "task_id": "impl-http-666", "status": "completed", "summary": "pushed",
            "branch": "agent/work", "commit": "a" * 40,
        })
        assert result.status_code == 200
    assert [pane for pane, _ in pushed] == ["%100", "%100"]
    assert "retry-impl-http-666-a1" in pushed[1][1]
    assert _reviews(TaskQueue(str(tmp_path / "http.db"))) == []
