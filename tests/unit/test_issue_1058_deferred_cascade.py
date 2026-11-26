"""Durable PR-state deferrals and review lineage admission (#1058)."""
import time

import pytest
from fastapi.testclient import TestClient

from agent_crew import github, pipeline
from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app, notify_lineage_end


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_CEA_MODE", "off")
    db = str(tmp_path / "tasks.db")
    q = TaskQueue(db)
    q.enqueue(TaskRequest("impl-1058", "implement", "work", branch="agent/1058",
                          context={"repo": "owner/repo", "risk_tier": 1,
                                   "agent_override": "codex", "pr_number": 1058}))
    q.submit_result("impl-1058", TaskResult("impl-1058", "completed", "done", pr_number=1058))
    state = {"value": "unknown"}
    monkeypatch.setattr(github, "pr_state", lambda *a, **k: state["value"])
    notices = []
    app = create_app(db, pane_map={}, watchdog_disabled=True, anomaly_disabled=True,
                     identity_required=False,
                     coordinator_notify_fn=lambda msg: notices.append(msg) or True)
    return q, app, state, notices


def due(q, *, aged=False):
    d = q.get_task_context("impl-1058")["deferred_cascade"]
    d["next_retry_at"] = 0
    if aged:
        d["first_deferred_at"] = time.time() - 7200
    q.patch_context("impl-1058", {"deferred_cascade": d})


@pytest.mark.parametrize("state", ["open", "merged", "closed"])
def test_unknown_then_recovered(case, state):
    q, app, live, notices = case
    with TestClient(app) as client:
        assert pipeline.auto_enqueue_review(q, "impl-1058", 1058) is None
        assert q.outbox_get("impl-1058")["state"] == "pending"
        notify_lineage_end(q, "impl-1058", notify_fn=lambda msg: notices.append(msg))
        assert not notices
        due(q)
        live["value"] = state
        client.post("/admin/replay-suppressed")
        client.post("/admin/replay-suppressed")
        reviews = [t for t in q.list_tasks() if t.task_type == "review"]
        assert [t.task_id for t in reviews] == (["review-impl-1058-r0"] if state == "open" else [])
        assert not q.get_task_context("impl-1058").get("deferred_cascade")
        if state == "closed":
            assert q.get_task("impl-1058").status == "needs_human"


def test_hand_built_review_deduplicated(case):
    q, app, live, _ = case
    with TestClient(app) as client:
        pipeline.auto_enqueue_review(q, "impl-1058", 1058)
        q.enqueue(TaskRequest("review-impl-1058-r0", "review", "manual", branch="agent/1058",
                              context={"prev_task_id": "impl-1058", "implementer_agent": "codex",
                                       "risk_tier": 1, "repo": "owner/repo", "pr_number": 1058}))
        due(q)
        live["value"] = "open"
        client.post("/admin/replay-suppressed")
        assert len([t for t in q.list_tasks() if t.task_type == "review"]) == 1


def test_backoff_and_one_notice(case):
    q, app, _, notices = case
    with TestClient(app) as client:
        pipeline.auto_enqueue_review(q, "impl-1058", 1058)
        client.post("/admin/replay-suppressed")
        assert q.get_task_context("impl-1058")["deferred_cascade"]["attempts"] == 1
        for attempt in (2, 3):
            due(q, aged=True)
            client.post("/admin/replay-suppressed")
            d = q.get_task_context("impl-1058")["deferred_cascade"]
            assert d["attempts"] == attempt
            assert d["next_retry_at"] > time.time() + 100
        assert len(notices) == 1
        assert "pr_state_unknown" in notices[0]


@pytest.mark.parametrize("context,missing", [
    ({}, "prev_task_id, implementer_agent"),
    ({"prev_task_id": "impl-1058"}, "implementer_agent"),
    ({"implementer_agent": "codex"}, "prev_task_id"),
])
def test_review_admission_requires_lineage(case, context, missing):
    q, _, _, _ = case
    with pytest.raises(ValueError, match=missing):
        q.enqueue(TaskRequest("manual", "review", "review",
                              context={"risk_tier": 1, **context}), ingress="http.tasks")


@pytest.mark.parametrize("root", [True, False])
def test_legacy_review_recovers_implementer(case, root):
    q, _, _, _ = case
    ctx = {"repo": "owner/repo", "pr_number": 1058, "risk_tier": 1}
    if root:
        ctx["lineage_root_task_id"] = "impl-1058"
    q.enqueue(TaskRequest("legacy-review", "review", "review", branch="agent/1058", context=ctx))
    q.submit_result("legacy-review", TaskResult("legacy-review", "completed", "fix",
                    pr_number=1058, verdict="request_changes", findings=["repair this"]))
    fix = pipeline.auto_enqueue_fix(q, "legacy-review", pr_state_fn=lambda _: "open")
    assert q.get_task(fix).context["agent_override"] == "codex"


def test_cascade_fix_keeps_codex(case):
    q, _, _, _ = case
    rid = pipeline.auto_enqueue_review(q, "impl-1058", 1058, pr_state_fn=lambda _: "open")
    q.submit_result(rid, TaskResult(rid, "completed", "fix", pr_number=1058,
                                   verdict="request_changes", findings=["repair this"]))
    fix = pipeline.auto_enqueue_fix(q, rid, pr_state_fn=lambda _: "open")
    assert fix == "fix-review-impl-1058-r0-r1"
    assert q.get_task(fix).context["agent_override"] == "codex"


@pytest.mark.parametrize("stage", ["test", "fix"])
def test_other_successors_share_durable_guard(case, stage):
    q, _, _, _ = case
    q.enqueue(TaskRequest("review-parent", "review", "review", branch="agent/1058",
                          context={"repo": "owner/repo", "pr_number": 1058, "risk_tier": 1,
                                   "prev_task_id": "impl-1058", "implementer_agent": "codex"}))
    q.submit_result("review-parent", TaskResult("review-parent", "completed", "review",
                    pr_number=1058, verdict="approve" if stage == "test" else "request_changes",
                    findings=[] if stage == "test" else ["repair this"]))
    enqueue = getattr(pipeline, "auto_enqueue_" + stage)
    assert enqueue(q, "review-parent", pr_state_fn=lambda _: "unknown") is None
    assert q.get_task_context("review-parent")["deferred_cascade"]["stage"] == stage
    assert q.outbox_get("review-parent")["state"] == "pending"
    assert enqueue(q, "review-parent", pr_state_fn=lambda _: "open")
    assert q.get_task_context("review-parent")["deferred_cascade"] is None


def test_unresolved_legacy_implementer_never_uses_role_default(case):
    q, _, _, _ = case
    q.enqueue(TaskRequest("orphan", "review", "review", branch="agent/orphan",
                          context={"risk_tier": 1, "pr_number": 999}))
    q.submit_result("orphan", TaskResult("orphan", "completed", "fix",
                    pr_number=999, verdict="request_changes", findings=["repair this"]))
    assert pipeline.auto_enqueue_fix(q, "orphan", pr_state_fn=lambda _: "open",
                                    pane_map={"implementer": "claude"}) is None
    assert not any(t.task_type == "implement" and t.task_id != "impl-1058" for t in q.list_tasks())


def test_repeated_deferral_preserves_replay_lease(case):
    q, _, _, _ = case
    pipeline.auto_enqueue_review(q, "impl-1058", 1058)
    assert q.outbox_claim("impl-1058", "first-sweep")
    q.defer_pr_cascade("impl-1058", stage="review", pr_number=1058)
    assert q.outbox_claim("impl-1058", "second-sweep") is None
    assert q.outbox_finish_claim("impl-1058", "first-sweep", state="pending")
    assert q.outbox_get("impl-1058")["state"] == "pending"


def test_http_review_refusal_names_missing_keys(case):
    _, app, _, _ = case
    with TestClient(app) as client:
        response = client.post("/tasks", json={
            "task_id": "missing-lineage", "task_type": "review", "description": "review",
            "pr_number": 1058, "context": {"risk_tier": 1, "repo": "owner/repo"},
        })
        assert response.status_code == 422
        assert "prev_task_id" in response.text
        assert "implementer_agent" in response.text


def test_dispatcher_review_recovers_attribution(case):
    q, _, _, _ = case
    q.enqueue(TaskRequest("triage-impl", "implement", "work",
                          context={"risk_tier": 1, "pr_number": 1059}))
    assert "implementer_agent" not in q.get_task("triage-impl").context
    q.record_attribution("triage-impl", agent="codex", role="implementer")
    rid = pipeline.auto_enqueue_review(q, "triage-impl", 1059, pane_map={},
                                       pr_state_fn=lambda _: "open")
    assert rid == "review-triage-impl-r0"
    assert q.get_task(rid).context["implementer_agent"] == "codex"


def test_legacy_stale_review_recovers_implementer(case):
    q, _, _, _ = case
    ctx = {"repo": "owner/repo", "pr_number": 1058, "risk_tier": 1,
           "prev_task_id": "impl-1058"}
    q.enqueue(TaskRequest("legacy-stale", "review", "review", context=ctx))
    q.submit_result("legacy-stale", TaskResult("legacy-stale", "completed", "done",
                                              pr_number=1058, verdict="approve"))
    rid = pipeline.requeue_review_at_head(q, "legacy-stale", 1058, "a" * 40,
                                          ctx, project="")
    assert rid is not None
    assert q.get_task(rid).context["implementer_agent"] == "codex"


def test_cli_review_with_explicit_implementer(case):
    from click.testing import CliRunner
    from agent_crew.cli import crew
    q, _, _, _ = case
    result = CliRunner().invoke(crew, [
        "enqueue", "review", "manual review", "--db", str(q.db_path),
        "--prev-task-id", "impl-1058", "--pr", "1058",
        "--reviewed-sha", "a" * 40, "--implementer-agent", "codex",
    ])
    assert result.exit_code == 0, result.output
    assert q.get_task(result.output.strip()).context["implementer_agent"] == "codex"


def test_unresolved_stale_review_records_refusal(case, monkeypatch):
    q, _, _, _ = case
    ctx = {"risk_tier": 1, "pr_number": 999}
    q.enqueue(TaskRequest("orphan-stale", "review", "review", context=ctx))
    refusals = []
    monkeypatch.setattr(pipeline, "_record_successor_refused",
                        lambda *args: refusals.append(args))
    assert pipeline.requeue_review_at_head(
        q, "orphan-stale", 999, "b" * 40, ctx, project="") is None
    assert refusals[0][2] == "implementer_unknown"
    assert not any(t.context.get("superseded_review") == "orphan-stale"
                   for t in q.list_tasks())


def test_dispatcher_review_uses_configured_implementer_without_attribution(case):
    q, _, _, _ = case
    q.enqueue(TaskRequest("mapped-impl", "implement", "work",
                          context={"risk_tier": 1, "pr_number": 1060}))
    rid = pipeline.auto_enqueue_review(
        q, "mapped-impl", 1060, pane_map={}, implementer_provider="claude",
        pr_state_fn=lambda _: "open")
    assert q.get_task(rid).context["implementer_agent"] == "claude"
