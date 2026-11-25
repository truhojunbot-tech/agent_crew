"""Review independence includes every implementation round (#1056)."""
import pytest

from agent_crew.protocol import TaskRequest, TaskResult
from agent_crew.queue import DuplicateReviewError, TaskQueue

SHA = "f690faed" + "0" * 32


@pytest.fixture
def queue(tmp_path):
    return TaskQueue(str(tmp_path / "tasks.db"))


def add(queue, task_id, kind, agent, *, pr=512, project="athena", **context):
    ctx = {"risk_tier": 1, **context}
    if kind == "review":
        ctx.update(reviewed_sha=SHA, agent_override=agent, implementer_agent="claude")
    queue.enqueue(TaskRequest(task_id=task_id, task_type=kind, description=task_id,
                              project=project, pr_number=pr, context=ctx))
    with queue._connect() as conn:
        conn.execute("UPDATE tasks SET dispatch_agent=? WHERE task_id=?", (agent, task_id))


def mixed(queue):
    add(queue, "impl-r0", "implement", "codex")
    add(queue, "fix-blocked", "implement", "codex")
    add(queue, "impl-split", "implement", "claude")
    add(queue, "fix-r1", "implement", "claude_cloud")
    with queue._connect() as conn:
        conn.execute("UPDATE tasks SET status='blocked' WHERE task_id='fix-blocked'")


def test_mixed_rounds_bind_only_independent_family(queue):
    mixed(queue)
    add(queue, "review", "review", "codex")
    assert queue.bind_independent_reviews("codex", ["codex", "claude_cloud", "chatgpt"]) == {
        "bound": ["review"], "blocked": []}
    assert queue.get_task_context("review")["agent_override"] == "chatgpt"


def test_mixed_rounds_refuse_claim_and_reroute(queue):
    mixed(queue)
    add(queue, "review", "review", "chatgpt")
    assert queue.reroute_stale_provider_bindings("chatgpt", (), reviewer_holder="codex") == []
    with queue._connect() as conn:
        conn.execute("UPDATE tasks SET context=json_remove(context, '$.agent_override') "
                     "WHERE task_id='review'")
    assert queue.dequeue(agent="codex", role="reviewer") is None
    assert queue.bind_independent_reviews("codex", ["codex", "claude_cloud"])["blocked"] == ["review"]


@pytest.mark.parametrize("completed", [False, True])
def test_pr512_nonindependent_review_never_survives(queue, completed):
    mixed(queue)
    add(queue, "review-codex", "review", "codex")
    if completed:
        queue.submit_result("review-codex", TaskResult(task_id="review-codex", status="completed",
                                                     summary="approved", verdict="approve"))
    add(queue, "review-chatgpt", "review", "chatgpt")
    assert queue.get_task_status("review-chatgpt") == "pending"
    with pytest.raises(DuplicateReviewError) as exc:
        add(queue, "review-third", "review", "chatgpt")
    assert exc.value.existing_task_id == "review-chatgpt"


def test_single_implementer_and_project_pr_isolation(queue):
    add(queue, "impl", "implement", "claude")
    add(queue, "other-pr", "implement", "codex", pr=513)
    add(queue, "other-project", "implement", "codex", project="other")
    add(queue, "review", "review", "codex")
    assert queue.bind_independent_reviews("codex", ["codex", "chatgpt"]) == {"bound": [], "blocked": []}
    with pytest.raises(DuplicateReviewError):
        add(queue, "duplicate", "review", "chatgpt")


def test_lineage_without_pr_and_missing_last_implementer(queue):
    add(queue, "root", "implement", "codex", pr=None)
    add(queue, "fix", "implement", "claude", pr=None, prev_task_id="root")
    add(queue, "review", "review", "codex", pr=None, prev_task_id="fix")
    with queue._connect() as conn:
        conn.execute("UPDATE tasks SET context=json_remove(context, '$.implementer_agent') "
                     "WHERE task_id='review'")
    assert queue.bind_independent_reviews("codex", ["claude_cloud", "chatgpt"])["bound"] == ["review"]
    assert queue.get_task_context("review")["agent_override"] == "chatgpt"


def test_completed_dispatch_attribution_wins_over_review_override(queue):
    mixed(queue)
    add(queue, "review-codex", "review", "codex")
    queue.submit_result("review-codex", TaskResult(task_id="review-codex", status="completed",
                                                 summary="approved", verdict="approve"))
    with queue._connect() as conn:
        conn.execute("UPDATE tasks SET context=json_set(context, '$.agent_override', 'chatgpt') "
                     "WHERE task_id='review-codex'")
    add(queue, "review-chatgpt", "review", "chatgpt")
    claimed = queue.dequeue(agent="chatgpt", role="reviewer")
    assert claimed.task_id == "review-chatgpt"
    assert queue.record_prepared_review_base(claimed.task_id, {"reviewed_sha": SHA})


def test_explicit_root_includes_prless_round_and_provider_alias(queue):
    add(queue, "root", "implement", "codex-cli", pr=None)
    add(queue, "fix", "implement", "claude", lineage_root_task_id="root")
    add(queue, "review", "review", "codex")
    assert queue.bind_independent_reviews("codex", ["codex", "chatgpt"])["bound"] == ["review"]


def test_session_continuity_does_not_add_unrelated_implementer(queue):
    add(queue, "unrelated", "implement", "codex", pr=999)
    add(queue, "impl", "implement", "claude", previous_task_id="unrelated")
    add(queue, "review", "review", "codex")
    assert queue.bind_independent_reviews("codex", ["codex", "chatgpt"]) == {
        "bound": [], "blocked": []}


@pytest.mark.parametrize("operation", ["dequeue", "bind", "reroute"])
def test_lineage_scan_once_per_project_per_transaction(queue, monkeypatch, operation):
    mixed(queue)
    for index in range(6):
        add(queue, f"review-{index}", "review", "codex")
    statements = []
    connect = queue._connect

    def traced_connect():
        conn = connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(queue, "_connect", traced_connect)
    if operation == "dequeue":
        assert queue.dequeue(agent="codex", role="reviewer") is None
    elif operation == "bind":
        queue.bind_independent_reviews("codex", ["chatgpt"])
    else:
        queue.reroute_stale_provider_bindings("codex", (), reviewer_holder="claude")
    scans = [sql for sql in statements
             if sql.startswith("SELECT task_id, task_type, pr_number,")]
    assert len(scans) == 1, scans


@pytest.mark.parametrize("reviewer,independent", [("codex", False), ("chatgpt", True)])
def test_merge_authority_checks_all_rounds(queue, monkeypatch, reviewer, independent):
    from agent_crew import github

    mixed(queue)
    add(queue, "review", "review", reviewer, prev_task_id="fix-r1")
    queue.record_attribution("fix-r1", agent="claude", task_type="implement")
    queue.record_attribution("review", agent=reviewer, task_type="review")
    with queue._connect() as conn:
        conn.execute("UPDATE tasks SET status='completed', verdict='approve' WHERE task_id='review'")
    monkeypatch.setattr(github, "pr_head_sha", lambda *a, **k: SHA)
    result = github.independent_review_for_head(queue, 512, "owner/repo")
    assert bool(result[0]) is independent, result


@pytest.mark.parametrize("reviewer,refused", [("codex", True), ("chatgpt", False)])
def test_dispatch_refusal_checks_all_rounds(tmp_path, queue, reviewer, refused):
    import json
    from fastapi.testclient import TestClient
    from agent_crew.server import create_app

    state = tmp_path / "state.json"
    state.write_text(json.dumps({"project": "athena"}))
    app = create_app(db_path=str(tmp_path / "tasks.db"), state_path=str(state),
                     watchdog_disabled=True, anomaly_disabled=True)
    mixed(queue)
    add(queue, "review", "review", reviewer, prev_task_id="fix-r1")
    queue.record_attribution("fix-r1", agent="claude", task_type="implement")
    review = queue.dequeue(role="reviewer")
    with TestClient(app):
        assert app.state.refuse_self_review(review, reviewer) is refused


def test_lineage_cache_is_not_reused_after_new_implementation(queue):
    add(queue, "first", "implement", "claude")
    add(queue, "review", "review", "codex")
    assert queue.bind_independent_reviews("codex", ["chatgpt"])["bound"] == []
    add(queue, "later", "implement", "codex")
    assert queue.bind_independent_reviews("codex", ["chatgpt"])["bound"] == ["review"]


def test_merge_requires_dispatch_evidence_not_context_hint(queue, monkeypatch):
    from agent_crew import github

    add(queue, "review", "review", "chatgpt")
    queue.record_attribution("review", agent="chatgpt", task_type="review")
    with queue._connect() as conn:
        conn.execute("UPDATE tasks SET status='completed', verdict='approve' WHERE task_id='review'")
    monkeypatch.setattr(github, "pr_head_sha", lambda *a, **k: SHA)
    assert github.independent_review_for_head(queue, 512, "owner/repo") == (
        "", "", "", "author or reviewer attribution unavailable")
