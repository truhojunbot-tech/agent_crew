"""#759: headless cloud implement routing, cost receipts and budget."""

import json
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app


def _state(tmp_path):
    roles = []
    for role, agent in (("implementer", "claude_cloud"), ("reviewer", "codex"),
                        ("tester", "gemini")):
        wt = tmp_path / role
        wt.mkdir()
        (wt / ".git").mkdir()
        roles.append({"role": role, "agent": agent, "worktree": str(wt)})
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"project": "agent_crew", "role_agents": {
        "implementer": "claude_cloud", "reviewer": "codex", "tester": "gemini"},
        "roles": roles}))
    return str(path)


def _credit(path, used):
    path.write_text(json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(),
                                "cloud_credit": {"used_dollars": used}}))


def _wait_for(predicate):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.02)
    assert predicate()


def test_headless_cloud_implement_reconciles_pr_and_records_cost(tmp_path, monkeypatch, caplog):
    source = TaskQueue(str(tmp_path / "source_tasks.db"))
    source.enqueue(TaskRequest(task_id="impl-cloud", task_type="implement",
                              description="Implement #759", branch="agent/impl-cloud",
                              project="agent_crew", context={"repo": "owner/repo"}))
    shutil.copy2(source._db_path, tmp_path / "tasks.db")
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    credit = tmp_path / "credit.json"
    _credit(credit, 12.0)
    monkeypatch.setenv("AGENT_CREW_CLOUD_CREDIT_FILE", str(credit))
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", ".02")
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_CONCURRENCY", "1")
    monkeypatch.setattr(cloud, "_validated_cloud_checkout",
                        lambda project, repo, state_path=None: ("/fixture/claude", repo))
    launches = []

    def run(argv, **kwargs):
        if "--help" in argv:
            return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
        launches.append(argv)
        _credit(credit, 12.75)
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "session_id": "session_abc12345", "url": "https://claude.ai/code/session_abc12345"}))

    monkeypatch.setattr(cloud, "_default_run", run)
    # Fake GitHub discovery; the existing review pipeline still creates the review.
    visible = [False]
    monkeypatch.setattr(cloud._github, "pr_number_for_branch",
                        lambda *a, **kw: 759 if visible[0] else None)
    monkeypatch.setattr(cloud._github, "branch_head_commit_message", lambda *a, **kw: None)
    monkeypatch.setattr(cloud._github, "pr_head_sha", lambda *a, **kw: "a" * 40)
    monkeypatch.setattr(cloud._github, "pr_state", lambda *a, **kw: "open")
    app = create_app(queue._db_path, state_path=_state(tmp_path),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        _wait_for(lambda: len(launches) == 1 and
                  queue.get_task_context("impl-cloud").get("cloud_session_id"))
        visible[0] = True
        outcomes = cloud.reconcile_all_cloud_tasks(queue)
        assert outcomes[0].action == "pr_ready"
        _wait_for(lambda: queue.get_task("impl-cloud").status == "completed")
        _wait_for(lambda: any("_dispatch_task enter role='reviewer'" in r.message
                              and "agent=codex" in r.message for r in caplog.records))
    task = queue.get_task("impl-cloud")
    assert task.pr_number == 759
    assert queue.get_attribution("impl-cloud")["agent"] == "claude_cloud"
    with sqlite3.connect(queue._db_path) as conn:
        assert conn.execute("SELECT dispatch_channel FROM tasks WHERE task_id='impl-cloud'").fetchone()[0] == "claude_cloud"
    assert queue.get_task_context("impl-cloud")["cloud_usd"] is None
    assert queue.get_task_context("impl-cloud")["cloud_credit_used_before"] == 12.0
    assert isinstance(queue.get_task_context("impl-cloud")["cloud_credit_before_fetched_at"], float)
    assert queue.get_task_context("impl-cloud")["cloud_credit_used_after"] is None
    assert queue.get_attribution("impl-cloud")["cloud_cost_status"] == "PENDING_SETTLE"
    reviews = [t for t in queue.list_tasks() if t.task_type == "review"]
    assert len(reviews) == 1
    assert reviews[0].context.get("agent_override") != "claude_cloud"


def test_cloud_usd_cap_leaves_second_implement_pending(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    for task_id in ("one", "two"):
        queue.enqueue(TaskRequest(task_id=task_id, task_type="implement",
                                  description="Implement", branch=f"agent/{task_id}",
                                  project="agent_crew", context={"repo": "owner/repo"}))
    credit = tmp_path / "credit.json"
    _credit(credit, 4.0)
    monkeypatch.setenv("AGENT_CREW_CLOUD_CREDIT_FILE", str(credit))
    monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_USD_PER_DAY", "1")
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_CONCURRENCY", "1")
    monkeypatch.setattr(cloud, "_validated_cloud_checkout",
                        lambda project, repo, state_path=None: ("/fixture/claude", repo))
    calls = []

    def run(argv, **kwargs):
        if "--help" in argv:
            return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "session_id": "session_abc12345", "url": "https://claude.ai/code/session_abc12345"}))

    assert cloud.dispatch_cloud_for_role(queue, role="implementer", task_type="implement", run_fn=run).dispatched
    _credit(credit, 5.2)
    monkeypatch.setattr(cloud._github, "pr_number_for_branch", lambda *a, **kw: 759)
    monkeypatch.setattr(cloud._github, "pr_head_sha", lambda *a, **kw: "a" * 40)
    monkeypatch.setattr(cloud._github, "pr_state", lambda *a, **kw: "open")
    cloud.reconcile_all_cloud_tasks(queue)
    monkeypatch.setenv("AGENT_CREW_CLOUD_COST_SETTLE_SECONDS", "0")
    queue.patch_context("one", {"cloud_ended_at": time.time() - 1})
    cloud.reconcile_all_cloud_tasks(queue)
    assert queue.get_attribution("one")["cloud_usd"] == 1.2
    outcome = cloud.dispatch_cloud_for_role(queue, role="implementer", task_type="implement", run_fn=run)
    assert outcome.skipped_reason == "usd_cap"
    assert queue.get_task("two").status == "pending"
    assert len(calls) == 1


def test_missing_or_stale_credit_is_unknown_and_does_not_block(tmp_path, monkeypatch):
    path = tmp_path / "credit.json"
    monkeypatch.setenv("AGENT_CREW_CLOUD_CREDIT_FILE", str(path))
    assert cloud.read_cloud_credit_used() is None
    path.write_text(json.dumps({"fetched_at": "2020-01-01T00:00:00Z",
                                "cloud_credit": {"used_dollars": 9}}))
    assert cloud.read_cloud_credit_used() is None


def test_existing_daily_launch_cap_applies_to_cloud_implement(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(task_id="later", task_type="implement", description="Implement",
                              branch="agent/later", project="agent_crew"))
    queue.record_attribution(task_id="earlier", agent="claude_cloud", role="implementer",
                             task_type="implement", provider_session_id="session_prior",
                             started_at=time.time())
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_PER_DAY", "1")
    outcome = cloud.dispatch_cloud_for_role(
        queue, role="implementer", task_type="implement",
        run_fn=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not launch")))
    assert outcome.skipped_reason == "daily_cap"
    assert queue.get_task("later").status == "pending"


def test_cloud_fix_waits_for_new_pr_head_before_re_review(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    old_head = "a" * 40
    new_head = "b" * 40
    head = [old_head]
    queue.enqueue(TaskRequest(
        task_id="fix-review-impl-r0-r1", task_type="implement",
        description="Fix PR #759 on the same branch", branch="agent/existing-pr",
        project="agent_crew", context={"risk_tier": 1, "repo": "owner/repo", "pr_number": 759,
                                       "fix_round": 1, "prev_task_id": "review-impl-r0"},
    ))
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_CONCURRENCY", "1")
    monkeypatch.setattr(cloud, "_validated_cloud_checkout",
                        lambda project, repo, state_path=None: ("/fixture/claude", repo))
    monkeypatch.setattr(cloud._github, "pr_number_for_branch", lambda *a, **kw: 759)
    monkeypatch.setattr(cloud._github, "pr_head_sha", lambda *a, **kw: head[0])
    monkeypatch.setattr(cloud._github, "pr_state", lambda *a, **kw: "open")
    monkeypatch.setattr(cloud._github, "branch_head_commit_message", lambda *a, **kw: None)

    def run(argv, **kwargs):
        if "--help" in argv:
            return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "session_id": "session_fix1234", "url": "https://claude.ai/code/session_fix1234"}))

    assert cloud.dispatch_cloud_for_role(queue, role="implementer", task_type="implement",
                                         run_fn=run).dispatched
    first = cloud.reconcile_all_cloud_tasks(queue)
    assert first[0].action == "still_dispatched"
    assert queue.get_task_context("fix-review-impl-r0-r1")["cloud_pr_head_at_launch"] == old_head
    assert queue.get_task("fix-review-impl-r0-r1").status == "in_progress"
    assert not [t for t in queue.list_tasks() if t.task_type == "review"]
    assert queue.get_task_context("fix-review-impl-r0-r1")["cloud_usd"] is None

    head[0] = ""  # A transient GitHub read must not turn into a new head.
    unavailable = cloud.reconcile_all_cloud_tasks(queue)
    assert unavailable[0].action == "still_dispatched"
    assert queue.get_task("fix-review-impl-r0-r1").status == "in_progress"

    head[0] = new_head
    second = cloud.reconcile_all_cloud_tasks(queue)
    assert second[0].action == "pr_ready"
    assert queue.get_task("fix-review-impl-r0-r1").status == "completed"
    reviews = [t for t in queue.list_tasks() if t.task_type == "review"]
    assert len(reviews) == 1
    assert reviews[0].context["reviewed_sha"] == new_head


def test_cloud_usd_cap_reserves_in_flight_implement(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    for task_id in ("first", "second"):
        queue.enqueue(TaskRequest(task_id=task_id, task_type="implement",
                                  description="Implement", branch=f"agent/{task_id}",
                                  project="agent_crew", context={"repo": "owner/repo"}))
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_USD_PER_DAY", "10")
    monkeypatch.setattr(cloud, "_validated_cloud_checkout",
                        lambda project, repo, state_path=None: ("/fixture/claude", repo))
    launches = []

    def run(argv, **kwargs):
        if "--help" in argv:
            return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
        launches.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "session_id": "session_first123", "url": "https://claude.ai/code/session_first123"}))

    assert cloud.dispatch_cloud_for_role(queue, role="implementer", task_type="implement",
                                         run_fn=run).dispatched
    blocked = cloud.dispatch_cloud_for_role(queue, role="implementer", task_type="implement",
                                            run_fn=run)
    assert blocked.skipped_reason == "usd_cap_reserved"
    assert queue.get_task("second").status == "pending"
    assert len(launches) == 1


def test_cloud_fix_accepts_new_terminal_signal_with_unchanged_pr_head(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(task_id="fix-terminal", task_type="implement",
                              description="Fix", branch="agent/existing-pr",
                              project="agent_crew", context={"repo": "owner/repo"}))
    queue.dequeue(role="implementer")
    queue.record_dispatch("fix-terminal", channel="claude_cloud", agent="claude_cloud",
                          target="pending")
    queue.patch_context("fix-terminal", {"cloud_pr_head_at_launch": "a" * 40,
                                         "cloud_branch_message_at_launch": "old commit"})
    task = queue.list_in_progress_by_dispatch_channel("claude_cloud")[0]
    outcome = cloud.reconcile_cloud_dispatch(
        queue, task, pr_number_for_branch_fn=lambda *a, **kw: 759,
        pr_head_sha_fn=lambda *a, **kw: "a" * 40,
        commit_message_fn=lambda *a, **kw: "ALREADY_FIXED verified without changes",
    )
    assert outcome.action == "already_fixed"
    assert queue.get_task("fix-terminal").status == "completed"


def test_cloud_fix_defers_when_launch_head_cannot_be_verified(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest(task_id="fix-unavailable", task_type="implement",
                              description="Fix", branch="agent/existing-pr",
                              project="agent_crew", context={"repo": "owner/repo",
                                                             "pr_number": 759, "fix_round": 1}))
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setattr(cloud._github, "pr_head_sha", lambda *a, **kw: None)
    monkeypatch.setattr(cloud._github, "branch_head_commit_message", lambda *a, **kw: None)

    def run(argv, **kwargs):
        if "--help" in argv:
            return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
        raise AssertionError("cloud session started without a baseline head")

    outcome = cloud.dispatch_cloud_for_role(queue, role="implementer",
                                            task_type="implement", run_fn=run)
    assert outcome.skipped_reason == "cloud_pr_head_unavailable"
    assert queue.get_task("fix-unavailable").status == "pending"
    assert queue.get_task_context("fix-unavailable")["push_not_before"] > time.time()
