"""#989: the cloud USD cap fails closed.

UNKNOWN (NULL) session costs are charged at a per-session estimate, a measured
cost replaces that estimate, reviews share the one daily budget, and a capped
or busy cloud review is HELD pending -- never handed to local Claude.
"""

import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app

SHA = "a" * 40


def _session(queue, task_id, *, role="implementer", usd=None, started_at=None,
             status=None):
    queue.record_attribution(
        task_id=task_id, project="agent_crew", agent="claude_cloud", role=role,
        task_type="review" if role == "reviewer" else "implement",
        provider_session_id=f"session_{task_id}", started_at=started_at or time.time())
    queue.record_cloud_cost(task_id, None, None, usd, status=status)


def _implement(queue, task_id="impl-next"):
    queue.enqueue(TaskRequest(task_id=task_id, task_type="implement",
                              description="Implement", branch=f"agent/{task_id}",
                              project="agent_crew", context={"repo": "owner/repo"}))


def _review(queue, task_id="review-cloud"):
    queue.enqueue(TaskRequest(
        task_id=task_id, task_type="review", description="Review pinned PR",
        branch=f"agent/{task_id}", project="agent_crew",
        context={"pr_number": 989, "reviewed_sha": SHA, "repo": "owner/repo",
                 "risk_tier": 2}))


def _no_launch(*_args, **_kwargs):
    raise AssertionError("cloud CLI invoked over the USD cap")


@pytest.fixture
def notices(monkeypatch):
    sent = []
    monkeypatch.setattr("agent_crew.server.coordinator_notice_once",
                        lambda *a, **kw: sent.append(a))
    return sent


@pytest.fixture(autouse=True)
def _cloud_env(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    for name in ("AGENT_CREW_CLOUD_MAX_USD_PER_DAY", "AGENT_CREW_CLOUD_MAX_PER_DAY",
                 "AGENT_CREW_CLOUD_UNKNOWN_SESSION_USD", "AGENT_CREW_CLOUD_SHADOW"):
        monkeypatch.delenv(name, raising=False)


def test_two_unknown_sessions_trip_cap_with_estimate(tmp_path, monkeypatch, notices, caplog):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _session(queue, "s1", status="UNKNOWN_OVERLAP")
    _session(queue, "s2", status="UNKNOWN_NO_FRESH_READING")
    _implement(queue)
    monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_USD_PER_DAY", "3.0")
    monkeypatch.setenv("AGENT_CREW_CLOUD_UNKNOWN_SESSION_USD", "2.0")
    with caplog.at_level(logging.WARNING, logger=cloud.logger.name):
        outcome = cloud.dispatch_cloud_for_role(queue, role="implementer",
                                                task_type="implement", run_fn=_no_launch)
    assert outcome.skipped_reason == "usd_cap"
    task = queue.get_task("impl-next")
    assert task.status == "pending"
    assert task.error_info["spent_measured"] == 0.0
    assert task.error_info["spent_estimated"] == 4.0
    assert task.error_info["limit"] == 3.0
    assert any("spent_measured=0.00 spent_estimated=4.00 limit=3.00" in r.getMessage()
               for r in caplog.records)


def test_default_estimate_is_two_usd(tmp_path, monkeypatch, notices):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _session(queue, "s1")
    _session(queue, "s2")
    _implement(queue)
    monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_USD_PER_DAY", "3.0")
    assert cloud.cloud_unknown_session_usd() == 2.0
    outcome = cloud.dispatch_cloud_for_role(queue, role="implementer",
                                            task_type="implement", run_fn=_no_launch)
    assert outcome.skipped_reason == "usd_cap"


@pytest.mark.parametrize("raw", ["abc", "-1", "nan"])
def test_invalid_estimate_fails_closed_to_default(monkeypatch, raw):
    monkeypatch.setenv("AGENT_CREW_CLOUD_UNKNOWN_SESSION_USD", raw)
    assert cloud.cloud_unknown_session_usd() == 2.0


def test_measured_cost_replaces_estimate(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    day_start = int(time.time() // 86400) * 86400
    _session(queue, "s1")
    _session(queue, "s2")
    assert cloud.cloud_usd_spent_today(queue, day_start) == (0.0, 4.0)
    queue.record_cloud_cost("s1", 4.0, 4.5, 0.5, status="MEASURED")
    # No double counting: s1 now contributes only its measured 0.5.
    assert cloud.cloud_usd_spent_today(queue, day_start) == (0.5, 2.0)
    monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_USD_PER_DAY", "3.0")
    assert cloud._usd_cap_tripped(queue, day_start, role="implementer") is None
    queue.record_cloud_cost("s2", 4.5, 5.0, 0.5, status="MEASURED")
    assert cloud.cloud_usd_spent_today(queue, day_start) == (1.0, 0.0)


def test_prior_day_and_not_launched_rows_are_not_charged(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    day_start = int(time.time() // 86400) * 86400
    _session(queue, "yesterday", started_at=day_start - 10)
    _session(queue, "preflight", status="NOT_LAUNCHED")
    queue.record_attribution(task_id="local", agent="codex", role="implementer",
                             task_type="implement", started_at=time.time())
    assert cloud.cloud_usd_spent_today(queue, day_start) == (0.0, 0.0)


def test_preflight_failure_before_launch_records_not_launched(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _implement(queue, "never-launched")
    task = queue.dequeue(agent="claude_cloud", role="implementer")
    cloud._fail_closed_dispatch(queue, task, "implement", "cloud_repo_unresolved")
    assert queue.get_attribution("never-launched")["cloud_cost_status"] == "NOT_LAUNCHED"
    day_start = int(time.time() // 86400) * 86400
    assert cloud.cloud_usd_spent_today(queue, day_start) == (0.0, 0.0)


def test_reviews_count_toward_implement_cap(tmp_path, monkeypatch, notices):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _session(queue, "review-a", role="reviewer")
    _session(queue, "cloud-shadow:review-b", role="reviewer")
    _implement(queue)
    monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_USD_PER_DAY", "3.0")
    outcome = cloud.dispatch_cloud_for_role(queue, role="implementer",
                                            task_type="implement", run_fn=_no_launch)
    assert outcome.skipped_reason == "usd_cap"


def test_review_launch_checks_shared_usd_cap_and_holds(tmp_path, monkeypatch, notices):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _session(queue, "impl-a")
    _session(queue, "impl-b", usd=1.5)
    _review(queue)
    monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_USD_PER_DAY", "3.0")
    for _ in range(3):
        outcome = cloud.dispatch_cloud_for_role(queue, role="reviewer",
                                                task_type="review", run_fn=_no_launch)
        assert outcome.skipped_reason == "usd_cap"
    task = queue.get_task("review-cloud")
    assert task.status == "pending"
    hold = task.context["cloud_review_hold"]
    assert hold["reason"] == "cloud_usd_cap"
    assert "spent_measured=1.50 spent_estimated=2.00 limit=3.00" in hold["detail"]
    assert [n[1:3] for n in notices] == [("review-cloud", "cloud_usd_cap_review_hold")]


def test_review_daily_cap_holds_with_typed_reason(tmp_path, monkeypatch, notices):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _review(queue)
    monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_PER_DAY", "0")
    for _ in range(2):
        assert cloud.dispatch_cloud_for_role(queue, role="reviewer", task_type="review",
                                             run_fn=_no_launch).skipped_reason == "daily_cap"
    task = queue.get_task("review-cloud")
    assert task.status == "pending"
    assert task.context["cloud_review_hold"]["reason"] == "cloud_daily_cap"
    assert len(notices) == 1


def test_review_at_capacity_holds_with_typed_reason(tmp_path, monkeypatch, notices):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _implement(queue, "busy")
    busy = queue.dequeue(agent="claude_cloud", role="implementer")
    queue.record_dispatch(busy.task_id, channel=cloud.DISPATCH_CHANNEL,
                          agent="claude_cloud", target="pending")
    _review(queue)
    monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_CONCURRENCY", "1")
    outcome = cloud.dispatch_cloud_for_role(queue, role="reviewer", task_type="review",
                                            run_fn=_no_launch)
    assert outcome.skipped_reason == "at_capacity"
    task = queue.get_task("review-cloud")
    assert task.status == "pending"
    assert task.context["cloud_review_hold"]["reason"] == "cloud_at_capacity"
    assert [n[2] for n in notices] == ["cloud_at_capacity_review_hold"]


def test_no_cap_env_launches_despite_unknown_sessions(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    for index in range(5):
        _session(queue, f"old-{index}")
    _implement(queue, "one")
    monkeypatch.setattr(cloud, "_validated_cloud_checkout",
                        lambda project, repo, state_path=None: ("/fixture/claude", repo))
    launches = []

    def run(argv, **kwargs):
        if "--help" in argv:
            return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
        launches.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "session_id": "session_abc12345",
            "url": "https://claude.ai/code/session_abc12345"}))

    outcome = cloud.dispatch_cloud_for_role(queue, role="implementer",
                                            task_type="implement", run_fn=run)
    assert outcome.dispatched
    assert len(launches) == 1


# --- dispatcher: a capped cloud review is never handed to local Claude -------

def _state(tmp_path):
    roles = []
    for role, agent in (("implementer", "codex"), ("reviewer", "claude_cloud"),
                        ("tester", "gemini")):
        worktree = tmp_path / f"wt_{role}"
        worktree.mkdir()
        (worktree / ".git").mkdir()
        roles.append({"role": role, "agent": agent, "worktree": str(worktree)})
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"project": "agent_crew", "role_agents": {
        "implementer": "codex", "reviewer": "claude_cloud", "tester": "script"},
        "roles": roles}))
    return str(path)


@pytest.mark.parametrize("cap_env, reason", [
    ("AGENT_CREW_CLOUD_MAX_PER_DAY", "cloud_daily_cap"),
    ("AGENT_CREW_CLOUD_MAX_USD_PER_DAY", "cloud_usd_cap"),
])
def test_dispatcher_capped_cloud_review_stays_pending_not_local_claude(
        tmp_path, monkeypatch, cap_env, reason):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _review(queue)
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", ".02")
    monkeypatch.setenv(cap_env, "0")
    monkeypatch.setattr(cloud, "_default_run", _no_launch)
    local_agents = []
    real_cap = cloud.dispatch_cloud_for_role
    ticks = threading.Event()
    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        if len(calls) >= 3:
            ticks.set()
        return real_cap(*args, **kwargs)

    def cooldown(_self, agent):
        # Seen only when a local dispatch is attempted; the cooldown stops it.
        local_agents.append(agent)
        return time.time() + 60

    monkeypatch.setattr(cloud, "dispatch_cloud_for_role", counted)
    monkeypatch.setattr("agent_crew.server.QuotaBudgetProvider.active_cooldown_until",
                        cooldown)
    app = create_app(queue._db_path, state_path=_state(tmp_path),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        assert ticks.wait(4)
    task = queue.get_task("review-cloud")
    assert task.status == "pending"
    assert task.context["cloud_review_hold"]["reason"] == reason
    assert task.context.get("agent_override") is None
    assert "claude" not in local_agents
    assert queue.get_result("review-cloud") is None


def test_dispatcher_at_capacity_review_stays_pending_not_local_claude(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _review(queue)
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", ".02")
    ticks = threading.Event()
    calls = []
    local_agents = []

    def at_capacity(*_args, **_kwargs):
        calls.append(1)
        if len(calls) >= 3:
            ticks.set()
        return SimpleNamespace(skipped_reason="at_capacity", dispatched=False)

    def cooldown(_self, agent):
        # Seen only when a local dispatch is attempted; the cooldown stops it.
        local_agents.append(agent)
        return time.time() + 60

    monkeypatch.setattr(cloud, "dispatch_cloud_for_role", at_capacity)
    monkeypatch.setattr("agent_crew.server.QuotaBudgetProvider.active_cooldown_until",
                        cooldown)
    app = create_app(queue._db_path, state_path=_state(tmp_path),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        assert ticks.wait(4)
    assert queue.get_task("review-cloud").status == "pending"
    assert "claude" not in local_agents


def test_shadow_review_launch_honours_shared_usd_cap(tmp_path, monkeypatch, caplog):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _session(queue, "impl-a")
    _session(queue, "impl-b")
    _review(queue)
    monkeypatch.setenv("AGENT_CREW_CLOUD_SHADOW", "1")
    monkeypatch.setenv("AGENT_CREW_CLOUD_MAX_USD_PER_DAY", "3.0")
    with caplog.at_level(logging.INFO, logger=cloud.logger.name):
        assert cloud.launch_shadow_review(queue, queue.get_task("review-cloud"),
                                          run_fn=_no_launch) is False
    assert any("shadow skip task=review-cloud reason=usd_cap" in r.getMessage()
               for r in caplog.records)
