"""#864 item 4: cloud sessions may only resume a cloud implement root."""

from types import SimpleNamespace

from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue


ROOT_SESSION = "01a0d898-codex-or-cloud-session"


def _lineage(tmp_path, *, root_agent: str, task_type: str):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    branch = "agent/864-work"
    queue.enqueue(TaskRequest(
        "impl-864-root", "implement", "Implement #864", branch=branch,
        project="agent_crew", context={"repo": "owner/repo", "risk_tier": 1},
    ))
    queue.dequeue(role="implementer")
    queue.record_attribution(task_id="impl-864-root", agent=root_agent,
                             provider_session_id=ROOT_SESSION)
    queue.submit_result("impl-864-root", TaskResult(
        "impl-864-root", "completed", "done", branch=branch, pr_number=864))
    task_id = "review-864" if task_type == "review" else "fix-864"
    queue.enqueue(TaskRequest(
        task_id, task_type, "Review PR" if task_type == "review" else "Fix PR",
        branch=branch, project="agent_crew", context={
            "repo": "owner/repo", "prev_task_id": "impl-864-root",
            "pr_number": 864, "reviewed_sha": "a" * 40,
            "agent_override": "claude_cloud", "risk_tier": 1,
        },
    ))
    return queue, queue.get_task(task_id)


def test_codex_root_cloud_review_launches_fresh(tmp_path, monkeypatch):
    queue, task = _lineage(tmp_path, root_agent="codex", task_type="review")
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setattr(cloud, "_validated_cloud_checkout",
                        lambda *args, **kwargs: ("/fixture/checkout", "owner/repo"))
    monkeypatch.setattr(cloud, "read_cloud_credit_used", lambda: None)
    launches = []

    def run(argv, **kwargs):
        if "--help" in argv:
            return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
        launches.append(argv)
        return SimpleNamespace(returncode=0, stdout=(
            '{"session_id":"new-claude-session",'
            '"url":"https://claude.ai/code/new-claude-session"}'))

    outcome = cloud.dispatch_cloud_for_role(
        queue, role="reviewer", task_type="review", run_fn=run)
    assert outcome.dispatched
    assert len(launches) == 1
    assert ROOT_SESSION not in " ".join(launches[0])
    assert queue.get_task(task.task_id).context["execution_policy"] == "fresh_cloud"


def test_cloud_root_cloud_fix_resumes(tmp_path):
    queue, task = _lineage(tmp_path, root_agent="claude_cloud", task_type="implement")
    assert cloud._resolve_resume_session_id(queue, task) == ROOT_SESSION


def test_codex_root_cloud_fix_launches_fresh(tmp_path):
    queue, task = _lineage(tmp_path, root_agent="codex", task_type="implement")
    assert cloud._resolve_resume_session_id(queue, task) is None


def test_cloud_root_cloud_review_launches_fresh(tmp_path):
    queue, task = _lineage(tmp_path, root_agent="claude_cloud", task_type="review")
    assert cloud._resolve_resume_session_id(queue, task) is None
