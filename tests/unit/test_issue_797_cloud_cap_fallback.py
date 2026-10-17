"""#797: capped cloud implement work is handed to a local worker visibly."""

import json
from types import SimpleNamespace

import pytest

from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue


def _state(tmp_path, *, local=True):
    path = tmp_path / "state.json"
    roles = [{"role": "implementer", "agent": "codex",
              "worktree": str(tmp_path / "codex")}] if local else []
    path.write_text(json.dumps({"project": "agent_crew", "role_agents": {
        "implementer": "claude_cloud", "reviewer": "codex" if local else "claude_cloud",
        "tester": "script"}, "roles": roles,
        "worktrees": {"codex": str(tmp_path / "codex")} if local else {}}))
    return str(path)


def _task(queue, *, task_id="fix-pr", context=None):
    queue.enqueue(TaskRequest(task_id=task_id, task_type="implement",
                              description="Fix PR", branch="agent/fix",
                              project="agent_crew", context=context or {}))


@pytest.mark.parametrize("cap_kind", ["daily", "usd"])
def test_cap_reroutes_one_task_with_durable_reason(tmp_path, monkeypatch, cap_kind):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _task(queue)
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    if cap_kind == "daily":
        monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_PER_DAY", "1")
        monkeypatch.setattr(queue, "count_cloud_implements_started_since", lambda _: 1)
    else:
        monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_USD_PER_DAY", "1")
        monkeypatch.setattr(queue, "cloud_implement_usd_since", lambda _: 1.2)
    state = _state(tmp_path)
    run = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("cloud launched"))
    first = cloud.dispatch_cloud_for_role(queue, role="implementer", task_type="implement",
                                           state_path=state, run_fn=run)
    second = cloud.dispatch_cloud_for_role(queue, role="implementer", task_type="implement",
                                            state_path=state, run_fn=run)
    assert first.skipped_reason in ("daily_cap", "usd_cap")
    assert second.skipped_reason in ("daily_cap", "usd_cap", "no_task")
    task = queue.get_task("fix-pr")
    assert task.status == "pending"
    assert task.context["agent_override"] == "codex"
    assert task.context["fallback_from_task_id"] == "fix-pr"
    assert task.error_info["reason"] == "cloud_daily_cap"
    assert task.error_info["limit"] == 1
    assert task.error_info["count"] >= 1
    claimed = queue.dequeue(agent="codex", role="reviewer")
    assert claimed.task_id == "fix-pr"


def test_cap_without_local_implementer_notifies_once(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _task(queue)
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_PER_DAY", "1")
    monkeypatch.setattr(queue, "count_cloud_implements_started_since", lambda _: 1)
    notices = []
    monkeypatch.setattr("agent_crew.server.coordinator_notice_once",
                        lambda *a, **kw: notices.append((a, kw)))
    state = _state(tmp_path, local=False)
    for _ in range(2):
        cloud.dispatch_cloud_for_role(queue, role="implementer", task_type="implement",
                                      state_path=state)
    assert len(notices) == 1
    assert notices[0][0][2] == "cloud_cap_no_local_implementer"
    assert queue.get_task("fix-pr").error_info["reason"] == "cloud_daily_cap"


def test_cloud_retry_attribution_carries_retry_of(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _task(queue, context={"repo": "owner/repo", "retry_attempt": 1})
    # Trusted successor context is stamped server-side after root admission.
    queue.patch_context("fix-pr", {"retry_of": "original",
                                   "original_task_id": "original",
                                   "fallback_from_task_id": "prior-failure"})
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setattr(cloud, "_validated_cloud_checkout",
                        lambda *a, **kw: (str(tmp_path), "owner/repo"))
    def run(argv, **kwargs):
        if "--help" in argv:
            return SimpleNamespace(returncode=0, stdout="--cloud")
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "session_id": "session_abc12345", "url": "https://claude.ai/code/session_abc12345"}))
    result = cloud.dispatch_cloud_for_role(queue, role="implementer", task_type="implement",
                                           run_fn=run)
    assert result.dispatched
    attribution = queue.get_attribution("fix-pr")
    assert attribution["retry_of"] == "original"
    assert attribution["fallback_of"] == "prior-failure"
