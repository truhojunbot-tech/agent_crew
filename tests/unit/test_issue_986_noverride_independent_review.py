"""#986 round 2: a review whose reviewer would be its implementer's provider is
bound to an independent dispatchable provider BEFORE any claim, or ends once as
BLOCKED(INDEPENDENT_REVIEW_UNAVAILABLE). It is never claimed by the
implementer's provider and refused into needs_human, and never left pending
with nobody able to dispatch it.

Role map in every test mirrors apify-forge 8109 (11-02):
implementer=claude_cloud, reviewer=codex, tester=script.
"""

import json
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_crew import claude_cloud as cloud
from agent_crew.protocol import TaskRequest
from agent_crew.queue import INDEPENDENT_REVIEW_UNAVAILABLE, TaskQueue
from agent_crew.server import create_app

SHA = "a" * 40
SESSION = "session_986abcde"


@pytest.fixture(autouse=True)
def _cloud_checkout_contract(monkeypatch):
    # Same fixture as #712: fake .git dirs; real-git checks live in #744.
    monkeypatch.setattr(cloud, "_validated_cloud_checkout",
                        lambda project, repo, state_path=None: ("/fixture/claude", repo))
    monkeypatch.setenv("AGENT_CREW_CLOUD_SHADOW", "0")
    monkeypatch.delenv("AGENT_CREW_CLOUD_MAX_PER_DAY", raising=False)
    # Reconciliation reads GitHub; these tests stop at the launch.
    monkeypatch.setattr(cloud, "reconcile_all_cloud_tasks", lambda *a, **k: [])


def _state(tmp_path):
    roles = []
    for role, agent in (("reviewer", "codex"),):
        wt = tmp_path / f"wt_{role}"
        wt.mkdir()
        (wt / ".git").mkdir()
        roles.append({"role": role, "agent": agent, "worktree": str(wt)})
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"project": "agent_crew", "role_agents": {
        "implementer": "claude_cloud", "reviewer": "codex", "tester": "script"},
        "roles": roles}))
    return str(path)


def _review(queue, task_id, implementer="codex", override=None, pr=199):
    context = {"pr_number": pr, "reviewed_sha": SHA, "repo": "owner/repo",
               "risk_tier": 2, "implementer_agent": implementer}
    if override:
        context["agent_override"] = override
    queue.enqueue(TaskRequest(
        task_id=task_id, task_type="review", description="Review pinned PR",
        branch=f"agent/{task_id}", project="agent_crew", context=context))


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    assert predicate()


def _fake_cloud_cli(monkeypatch):
    launches = []

    def run(argv, **kwargs):
        if "--help" in argv:
            return SimpleNamespace(returncode=0, stdout="  --cloud  Run remotely\n")
        launches.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "session_id": SESSION, "url": f"https://claude.ai/code/{SESSION}"}))

    monkeypatch.setattr(cloud, "_default_run", run)
    return launches


def _record_adapter(monkeypatch):
    """Wrap the real cloud adapter; record every call's kwargs."""
    calls = []
    lock = threading.Lock()
    real = cloud.dispatch_cloud_for_role

    def recording(*args, **kwargs):
        with lock:
            calls.append(dict(kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(cloud, "dispatch_cloud_for_role", recording)
    return calls


def _dispatcher_env(monkeypatch, cloud_enabled):
    monkeypatch.setenv("AGENT_CREW_DISPATCHER", "1")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_INTERVAL", ".02")
    if cloud_enabled:
        monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    else:
        monkeypatch.delenv("AGENT_CREW_CLOUD_ENABLED", raising=False)


def _codex_never_claimed(monkeypatch):
    """Any codex claim of a review would be a #986 regression."""
    claims = []
    real = TaskQueue.dequeue

    def dequeue(self, agent="", role="", **kwargs):
        task = real(self, agent, role, **kwargs)
        if task is not None and task.task_type == "review":
            claims.append((agent, task.task_id))
        return task

    monkeypatch.setattr(TaskQueue, "dequeue", dequeue)
    return claims


# ── claim guard ────────────────────────────────────────────────────────────


def test_codex_cannot_claim_a_codex_implemented_review(tmp_path):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _review(queue, "review-self", implementer="codex")
    _review(queue, "review-cloud-impl", implementer="claude_cloud", pr=200)
    claimed = queue.dequeue(agent="codex", role="reviewer", claimed_via="dispatcher")
    # The ordinary (independent) review is still claimed by the role holder.
    assert claimed is not None and claimed.task_id == "review-cloud-impl"
    assert queue.dequeue(agent="codex", role="reviewer", claimed_via="dispatcher") is None
    assert queue.get_task("review-self").status == "pending"
    assert queue.get_result("review-self") is None


# ── (a) no override ────────────────────────────────────────────────────────


def test_a_no_override_review_is_bound_to_cloud_and_dispatched(tmp_path, monkeypatch):
    """apify-forge 8109 review-impl-198svc-post986: no override, implementer
    codex, reviewer codex. Must be dispatched on claude_cloud, not codex."""
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _review(queue, "review-post986", implementer="codex")
    _dispatcher_env(monkeypatch, cloud_enabled=True)
    launches = _fake_cloud_cli(monkeypatch)
    calls = _record_adapter(monkeypatch)
    claims = _codex_never_claimed(monkeypatch)
    app = create_app(queue._db_path, state_path=_state(tmp_path),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        _wait_for(lambda: queue.get_task("review-post986").status == "in_progress"
                  and queue.get_attribution("review-post986") is not None)
    assert len(launches) == 1
    assert queue.get_attribution("review-post986")["agent"] == "claude_cloud"
    context = queue.get_task_context("review-post986")
    assert context["agent_override"] == "claude_cloud"
    assert context["independent_review_binding"]["provider"] == "claude_cloud"
    assert context["independent_review_binding"]["replaced"] == "codex"
    assert context["cloud_session_id"] == SESSION
    assert any(c.get("independent_only") and c.get("role") == "reviewer" for c in calls)
    assert ("codex", "review-post986") not in claims
    assert queue.get_result("review-post986") is None


def test_a_no_override_review_without_independent_reviewer_blocks_once(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _review(queue, "review-post986", implementer="codex", pr=199)
    _dispatcher_env(monkeypatch, cloud_enabled=False)
    claims = _codex_never_claimed(monkeypatch)
    notices = []
    app = create_app(queue._db_path, state_path=_state(tmp_path),
                     watchdog_disabled=True, anomaly_disabled=True,
                     coordinator_notify_fn=lambda message: notices.append(message) or True)
    with TestClient(app):
        _wait_for(lambda: queue.get_task("review-post986").status == "blocked")
        time.sleep(.3)  # many more ticks: still one block, one notice
    task = queue.get_task("review-post986")
    assert task.status == "blocked"
    result = queue.get_result("review-post986")
    assert result.summary.startswith(INDEPENDENT_REVIEW_UNAVAILABLE)
    block = result.error_info
    assert block["reason"] == INDEPENDENT_REVIEW_UNAVAILABLE
    assert block["op"] == "review" and block["task_id"] == "review-post986"
    assert block["pr_number"] == 199
    assert block["implementer_agent"] == "codex" and block["reviewer_agent"] == "codex"
    assert block["owner"] and block["resume_trigger"]
    mine = [n for n in notices if "kind=independent_review_unavailable" in n]
    assert len(mine) == 1 and "task=review-post986" in mine[0]
    assert claims == []
    assert result.status != "needs_human"


# ── (b) override=claude_cloud while cloud holds only implementer ────────────


def test_b_cloud_override_without_reviewer_role_dispatches_on_cloud(tmp_path, monkeypatch):
    """PR #987 kept agent_override=claude_cloud; on 8109 the dispatcher never
    called the cloud review adapter because cloud held only implementer."""
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _review(queue, "review-post986b", implementer="codex", override="claude_cloud")
    _dispatcher_env(monkeypatch, cloud_enabled=True)
    launches = _fake_cloud_cli(monkeypatch)
    calls = _record_adapter(monkeypatch)
    claims = _codex_never_claimed(monkeypatch)
    app = create_app(queue._db_path, state_path=_state(tmp_path),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        _wait_for(lambda: queue.get_task("review-post986b").status == "in_progress"
                  and queue.get_attribution("review-post986b") is not None)
    assert len(launches) == 1
    assert queue.get_attribution("review-post986b")["agent"] == "claude_cloud"
    review_calls = [c for c in calls if c.get("task_type") == "review"]
    assert review_calls and all(c.get("independent_only") for c in review_calls)
    context = queue.get_task_context("review-post986b")
    assert context["independent_review_binding"]["provider"] == "claude_cloud"
    assert context["independent_review_binding"]["replaced"] is None
    assert claims == [("claude_cloud", "review-post986b")]


def test_b_cloud_override_with_cloud_disabled_blocks_once(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _review(queue, "review-post986b", implementer="codex", override="claude_cloud")
    _dispatcher_env(monkeypatch, cloud_enabled=False)
    claims = _codex_never_claimed(monkeypatch)
    notices = []
    app = create_app(queue._db_path, state_path=_state(tmp_path),
                     watchdog_disabled=True, anomaly_disabled=True,
                     coordinator_notify_fn=lambda message: notices.append(message) or True)
    with TestClient(app):
        _wait_for(lambda: queue.get_task("review-post986b").status == "blocked")
        time.sleep(.3)
    result = queue.get_result("review-post986b")
    assert result.error_info["reason"] == INDEPENDENT_REVIEW_UNAVAILABLE
    assert result.error_info["reviewer_agent"] == "claude_cloud"
    assert len([n for n in notices if "kind=independent_review_unavailable" in n]) == 1
    assert claims == []


def test_b_bound_review_blocks_once_when_cloud_cli_cannot_run(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _review(queue, "review-cli", implementer="codex", override="claude_cloud")
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    monkeypatch.setattr(cloud, "probe_cloud_cli",
                        lambda *a, **k: cloud.CloudCapability(False, "probe failed"))
    notices = []
    app = create_app(queue._db_path, state_path=_state(tmp_path),
                     watchdog_disabled=True, anomaly_disabled=True,
                     coordinator_notify_fn=lambda message: notices.append(message) or True)
    with TestClient(app):
        assert app.state.bind_independent_reviews()["bound"] == ["review-cli"]
        import asyncio
        asyncio.run(app.state.dispatch_independent_cloud_reviews())
        asyncio.run(app.state.dispatch_independent_cloud_reviews())
    result = queue.get_result("review-cli")
    assert queue.get_task("review-cli").status == "blocked"
    assert result.error_info["reason"] == INDEPENDENT_REVIEW_UNAVAILABLE
    assert "cli_unsupported" in result.error_info["detail"]
    assert len([n for n in notices if "kind=independent_review_unavailable" in n]) == 1


# ── reroute / retry stay independent ───────────────────────────────────────


def test_reroute_and_retry_keep_the_review_independent(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _review(queue, "review-retry", implementer="codex")
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    app = create_app(queue._db_path, state_path=_state(tmp_path),
                     watchdog_disabled=True, anomaly_disabled=True)
    with TestClient(app):
        assert app.state.bind_independent_reviews()["bound"] == ["review-retry"]
        # #891 reroute keeps it (PR #987), a second sweep is a no-op.
        assert app.state.reroute_stale_bindings() == []
        assert app.state.bind_independent_reviews() == {"bound": [], "blocked": []}
        # Retry: claimed by cloud, requeued -> still bound, codex still cannot take it.
        claimed = queue.dequeue(agent="claude_cloud", role="reviewer",
                                claimed_via="cloud_push", independent_review_only=True)
        assert claimed is not None and claimed.task_id == "review-retry"
        queue.requeue("review-retry")
        assert queue.get_task("review-retry").context["agent_override"] == "claude_cloud"
        assert queue.dequeue(agent="codex", role="reviewer", claimed_via="dispatcher") is None
        # Cloud goes away: the binding is re-chosen, never dropped onto codex.
        monkeypatch.delenv("AGENT_CREW_CLOUD_ENABLED")
        assert app.state.bind_independent_reviews()["blocked"] == ["review-retry"]
    assert queue.get_result("review-retry").error_info["reason"] == INDEPENDENT_REVIEW_UNAVAILABLE


def test_independent_only_claim_takes_nothing_else(tmp_path):
    """Cloud without the reviewer role claims bound reviews only."""
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _review(queue, "review-plain", implementer="claude", override="claude_cloud")
    _review(queue, "review-unbound", implementer="codex", pr=200)
    assert queue.dequeue(agent="claude_cloud", role="reviewer", claimed_via="cloud_push",
                         independent_review_only=True) is None
    assert queue.dequeue(agent="claude_cloud", role="implementer", claimed_via="cloud_push",
                         independent_review_only=True) is None


# ── ordinary reviews unchanged ─────────────────────────────────────────────


def test_ordinary_review_is_untouched_and_claimed_by_role_holder(tmp_path, monkeypatch):
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    _review(queue, "review-ordinary", implementer="claude_cloud")
    monkeypatch.setenv("AGENT_CREW_CLOUD_ENABLED", "1")
    app = create_app(queue._db_path, state_path=_state(tmp_path),
                     watchdog_disabled=True, anomaly_disabled=True)
    before = queue.get_task_context("review-ordinary")
    with TestClient(app):
        assert app.state.bind_independent_reviews() == {"bound": [], "blocked": []}
    assert queue.get_task_context("review-ordinary") == before
    claimed = queue.dequeue(agent="codex", role="reviewer", claimed_via="dispatcher")
    assert claimed is not None and claimed.task_id == "review-ordinary"
