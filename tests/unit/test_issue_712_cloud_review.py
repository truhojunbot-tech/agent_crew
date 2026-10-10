"""#712: cloud review uses the existing review result and cascade contract."""

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app

SHA = "a" * 40
OTHER_SHA = "b" * 40
PR = 712
REPO = "owner/repo"
MARKER = f"<!-- agent_crew:cloud-review task=review-712 sha={SHA} -->"
FINDING = "HIGH src/agent_crew/queue.py:42 - Preserve the review cascade"


@pytest.fixture
def queue(tmp_db):
    return TaskQueue(tmp_db)


def _review(queue):
    queue.enqueue(TaskRequest(
        task_id="review-712", task_type="review", description="Review the pinned PR",
        branch="agent/reviewed", project="agent_crew",
        context={"pr_number": PR, "reviewed_sha": SHA, "repo": REPO, "risk_tier": 2},
    ))
    task = queue.dequeue(role="reviewer")
    queue.record_dispatch(task.task_id, channel=cloud.DISPATCH_CHANNEL,
                          agent=cloud.CLOUD_PROVIDER_NAME, target="pending")
    return task


def _comment(verdict="approve", sha=SHA, findings=""):
    return (f"[agent_crew review] verdict: {verdict}\n"
            f"reviewed_sha {sha}\n"
            f"Summary: Reviewed the pinned code and checked the relevant tests.\n"
            f"{findings}"
            f"<!-- agent_crew:cloud-review task=review-712 sha={sha} -->")


def test_review_prompt_contains_pinned_contract(queue):
    task = _review(queue)
    prompt = cloud.build_cloud_task_prompt(task, repo=REPO)
    for expected in (REPO, str(PR), SHA, task.description, MARKER,
                     "[agent_crew review] verdict: approve|request_changes",
                     "reviewed_sha", "HIGH|MED|LOW", "no push", "no merge"):
        assert expected.lower() in prompt.lower()
    assert "PR_READY" not in prompt


@pytest.mark.parametrize("verdict,findings", [
    ("approve", ""), ("request_changes", f"- {FINDING}\n"),
])
def test_review_comment_reconciles_through_result_handler(queue, verdict, findings):
    task = _review(queue)
    submitted = []
    outcome = cloud.reconcile_cloud_dispatch(
        queue, task,
        pr_comments_fn=lambda pr, repo=None: [{"body": _comment(verdict, findings=findings)}],
        submit_review_result_fn=lambda task_id, result: submitted.append((task_id, result)) or {"status": "ok"},
    )
    assert outcome.action == "review_completed"
    assert len(submitted) == 1
    task_id, result = submitted[0]
    assert task_id == task.task_id
    assert result.verdict == verdict
    assert result.pr_number == PR
    assert result.findings == ([FINDING] if verdict == "request_changes" else [])


def test_wrong_marker_sha_fails_closed_and_uses_fallback(queue, monkeypatch):
    task = _review(queue)
    calls = []
    monkeypatch.setattr("agent_crew.pipeline.auto_fallback_failed_task",
                        lambda *args, **kwargs: calls.append(args))
    outcome = cloud.reconcile_cloud_dispatch(
        queue, task,
        pr_comments_fn=lambda pr, repo=None: [{"body": _comment(sha=OTHER_SHA)}],
        submit_review_result_fn=lambda *_: pytest.fail("wrong SHA submitted"),
    )
    assert outcome.action == "failed"
    assert queue.get_task(task.task_id).status == "failed"
    assert len(calls) == 1


@pytest.mark.parametrize("body", [
    _comment().replace(f"reviewed_sha {SHA}", f"reviewed_sha {OTHER_SHA}"),
    _comment("request_changes"),  # no actionable finding
    _comment("approve", findings=f"- {FINDING}\n"),
])
def test_malformed_review_comment_fails_closed(queue, monkeypatch, body):
    task = _review(queue)
    monkeypatch.setattr("agent_crew.pipeline.auto_fallback_failed_task",
                        lambda *args, **kwargs: None)
    outcome = cloud.reconcile_cloud_dispatch(
        queue, task, pr_comments_fn=lambda pr, repo=None: [{"body": body}],
        submit_review_result_fn=lambda *_: pytest.fail("malformed review submitted"),
    )
    assert outcome.action == "failed"
    assert queue.get_task(task.task_id).status == "failed"


def test_stale_review_without_comment_uses_fallback(queue, monkeypatch):
    task = _review(queue)
    monkeypatch.setenv(cloud._ENV_STALE_SECONDS, "1")
    monkeypatch.setattr(queue, "get_dispatched_at", lambda _: time.time() - 5)
    monkeypatch.setattr("agent_crew.pipeline.auto_fallback_failed_task",
                        lambda *args, **kwargs: None)
    outcome = cloud.reconcile_cloud_dispatch(
        queue, task, pr_comments_fn=lambda pr, repo=None: [],
        submit_review_result_fn=lambda *_: pytest.fail("stale review submitted"),
    )
    assert outcome.action == "failed"


def test_cloud_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv(cloud._ENV_ENABLED, raising=False)
    assert cloud.cloud_dispatch_enabled() is False


def test_cloud_and_local_review_use_same_server_fix_cascade(tmp_path, monkeypatch):
    """The cloud comment becomes a normal review result; it is not reposted."""
    import agent_crew.server as server
    fix_calls = []
    published = []
    monkeypatch.setattr(server, "review_publication_decision",
                        lambda *_a, **_kw: SimpleNamespace(publish=True))
    monkeypatch.setattr(server, "_pipeline_auto_enqueue_fix",
                        lambda queue, task_id, **_kw: fix_calls.append(task_id) or None)
    monkeypatch.setattr("agent_crew.github.post_review_comment",
                        lambda **kw: published.append(kw) or True)

    for cloud_path in (False, True):
        queue = TaskQueue(str(tmp_path / ("cloud.db" if cloud_path else "local.db")))
        task = _review(queue)
        app = create_app(db_path=queue._db_path, project="agent_crew",
                         watchdog_disabled=True)
        with TestClient(app) as client:
            def submit(task_id, result):
                response = client.post(
                    f"/tasks/{task_id}/result", json=result.__dict__,
                    headers={"X-Agent-Crew-Project": "agent_crew"})
                assert response.status_code == 200, response.text
                return response.json()

            if cloud_path:
                outcome = cloud.reconcile_cloud_dispatch(
                    queue, task,
                    pr_comments_fn=lambda pr, repo=None: [
                        {"body": _comment("request_changes", findings=f"- {FINDING}\n")}],
                    submit_review_result_fn=submit,
                )
                assert outcome.action == "review_completed"
            else:
                result = cloud.parse_cloud_review_comment(
                    _comment("request_changes", findings=f"- {FINDING}\n"),
                    task_id=task.task_id, reviewed_sha=SHA, pr_number=PR)
                submit(task.task_id, result)
        assert queue.get_task(task.task_id).status == "completed"

    assert fix_calls == ["review-712", "review-712"]
    assert len(published) == 1  # local handler only; cloud posted its own comment
