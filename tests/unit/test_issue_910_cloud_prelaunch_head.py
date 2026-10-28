"""#910: every cloud implement records the pre-launch PR head of its branch."""

import json
from types import SimpleNamespace

from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


def _run(argv, **kwargs):
    if "--help" in argv:
        return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
    return SimpleNamespace(returncode=0, stdout=json.dumps({
        "session_id": "session_cont1234", "url": "https://claude.ai/code/session_cont1234"}))


def _cloud_env(monkeypatch, head):
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_CONCURRENCY", "1")
    monkeypatch.setattr(cloud, "_validated_cloud_checkout",
                        lambda project, repo, state_path=None: ("/fixture/claude", repo))
    monkeypatch.setattr(cloud._github, "pr_number_for_branch", lambda *a, **kw: 282)
    monkeypatch.setattr(cloud._github, "pr_head_sha", lambda *a, **kw: head[0])
    monkeypatch.setattr(cloud._github, "pr_state", lambda *a, **kw: "open")
    monkeypatch.setattr(cloud._github, "branch_head_commit_message",
                        lambda *a, **kw: "previous round commit")


def test_continuation_without_pr_number_waits_for_new_pr_head(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    old_head, new_head = "a" * 40, "b" * 40
    head = [old_head]
    queue.enqueue(TaskRequest(
        task_id="impl-continuation", task_type="implement",
        description="Rebase continuation", branch="fix/275f-pace-alert-runtime",
        project="agent_crew", context={"repo": "owner/repo", "rebase_onto": "main"},
    ))
    _cloud_env(monkeypatch, head)

    assert cloud.dispatch_cloud_for_role(queue, role="implementer", task_type="implement",
                                         run_fn=_run).dispatched
    ctx = queue.get_task_context("impl-continuation")
    assert ctx["cloud_pr_head_at_launch"] == old_head
    assert ctx["cloud_pr_number_at_launch"] == 282

    first = cloud.reconcile_all_cloud_tasks(queue)
    assert first[0].action == "still_dispatched"
    assert queue.get_task("impl-continuation").status == "in_progress"
    assert not [t for t in queue.list_tasks() if t.task_type == "review"]

    head[0] = new_head
    second = cloud.reconcile_all_cloud_tasks(queue)
    assert second[0].action == "pr_ready"
    task = queue.get_task("impl-continuation")
    assert task.status == "completed"
    reviews = [t for t in queue.list_tasks() if t.task_type == "review"]
    assert len(reviews) == 1
    assert reviews[0].context["reviewed_sha"] == new_head


def test_failed_review_enqueue_after_discovery_raises_notice(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(task_id="impl-noreview", task_type="implement",
                              description="Implement", branch="agent/impl-noreview",
                              project="agent_crew", context={"repo": "owner/repo"}))
    queue.dequeue(role="implementer")
    queue.record_dispatch("impl-noreview", channel="claude_cloud", agent="claude_cloud",
                          target="pending")
    notices = []
    import agent_crew.pipeline as pipeline
    import agent_crew.server as server
    monkeypatch.setattr(pipeline, "auto_enqueue_review", lambda *a, **kw: None)
    monkeypatch.setattr(server, "coordinator_notice_once",
                        lambda q, task_id, kind, detail="", **kw:
                        notices.append((task_id, kind, detail)))
    task = queue.list_in_progress_by_dispatch_channel("claude_cloud")[0]
    outcome = cloud.reconcile_cloud_dispatch(
        queue, task, pr_number_for_branch_fn=lambda *a, **kw: 759,
        pr_head_sha_fn=lambda *a, **kw: "c" * 40,
        commit_message_fn=lambda *a, **kw: None,
    )
    assert outcome.action == "still_dispatched"
    assert queue.get_task("impl-noreview").status == "in_progress"
    assert [(t, k) for t, k, _ in notices] == [("impl-noreview", "cloud_review_enqueue_failed")]
    assert "pr=759" in notices[0][2]


def test_prelaunch_pr_lookup_failure_defers_instead_of_launching(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    head = ["a" * 40]
    queue.enqueue(TaskRequest(
        task_id="impl-lookup-fail", task_type="implement",
        description="Rebase continuation", branch="fix/275f-pace-alert-runtime",
        project="agent_crew", context={"repo": "owner/repo", "rebase_onto": "main"},
    ))
    _cloud_env(monkeypatch, head)
    launches = []

    def run(argv, **kwargs):
        if "--help" not in argv:
            launches.append(argv)
        return _run(argv, **kwargs)

    def lookup_fails(*a, **kw):
        raise RuntimeError("gh: rate limited")

    monkeypatch.setattr(cloud._github, "pr_number_for_branch", lookup_fails)
    outcome = cloud.dispatch_cloud_for_role(queue, role="implementer", task_type="implement",
                                            run_fn=run)
    assert not outcome.dispatched
    assert outcome.skipped_reason == "cloud_pr_lookup_unavailable"
    assert launches == []
    task = queue.get_task("impl-lookup-fail")
    assert task.status == "pending"
    assert queue.get_task_context("impl-lookup-fail")["push_not_before"] > 0

    # Lookup recovers and the branch's existing PR is found: the launch must
    # now carry that PR's head, so reconcile does not mistake it for output.
    monkeypatch.setattr(cloud._github, "pr_number_for_branch", lambda *a, **kw: 282)
    monkeypatch.setattr(cloud.time, "time", lambda: 1e12)
    assert cloud.dispatch_cloud_for_role(queue, role="implementer", task_type="implement",
                                         run_fn=run).dispatched
    assert queue.get_task_context("impl-lookup-fail")["cloud_pr_head_at_launch"] == head[0]
    assert cloud.reconcile_all_cloud_tasks(queue)[0].action == "still_dispatched"
    assert queue.get_task("impl-lookup-fail").status == "in_progress"
