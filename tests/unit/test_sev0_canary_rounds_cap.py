"""D-11832: only a fresh quota-core decision may narrow one pinned lineage."""
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from agent_crew.pipeline import (_canary_round_cap, auto_enqueue_fix,
                                 auto_enqueue_test, resume_tier3_gate)
from agent_crew.protocol import GateRequest, TaskRequest, TaskResult
from agent_crew.queue import TaskQueue
from agent_crew.server import create_app
from agent_crew.tokenomics_canary import CANARY_ENV, ROUNDS_CAP_ENV
from fastapi.testclient import TestClient

ROOT = "impl-canary-rounds"


@pytest.fixture
def q(tmp_db):
    queue = TaskQueue(tmp_db)
    queue.enqueue(TaskRequest(task_id=ROOT, task_type="implement",
                              description="root", branch="canary-branch"))
    # A cap may cite only a decision published after the latest implement result.
    queue.submit_result(ROOT, TaskResult(ROOT, "completed", "root completed"))
    return queue


def _contract(tmp_path, monkeypatch, recommended=1, *, age=0, produced=True,
              mtime_age=0, task_id=ROOT):
    path = tmp_path / "policy.json"
    contract = {
        "contract_version": "1.0", "mode": "shadow",
        "decisions": [{"task_id": task_id,
                       "recommended_max_review_fix_rounds": recommended}],
    }
    if produced:
        contract["produced_at"] = (datetime.now(timezone.utc) - timedelta(days=age)).isoformat()
    path.write_text(json.dumps(contract))
    if mtime_age:
        timestamp = (datetime.now(timezone.utc) - timedelta(days=mtime_age)).timestamp()
        os.utime(path, (timestamp, timestamp))
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(path))


def _review(q, task_id="review-canary-rounds", *, root=ROOT, fix_round=1,
            verdict="request_changes", refresh_contract=True):
    q.enqueue(TaskRequest(task_id=task_id, task_type="review", description="review",
                          branch="canary-branch",
                          context={"prev_task_id": root, "fix_round": fix_round,
                                   "pr_number": 51}))
    q.submit_result(task_id, TaskResult(
        task_id=task_id, status="completed", summary="fix it", verdict=verdict,
        findings=["Fix the review finding"] if verdict == "request_changes" else [],
        pr_number=51))
    # Most cases model an emitter that observes the just-posted verdict before
    # the cascade evaluates. Tests for pre-verdict evidence opt out explicitly.
    policy_path = os.environ.get("AGENT_CREW_TOKENOMICS_POLICY_PATH")
    if refresh_contract and policy_path and os.path.exists(policy_path):
        path = os.fspath(policy_path)
        with open(path) as handle:
            contract = json.load(handle)
        produced = contract.get("produced_at")
        observed = (datetime.fromisoformat(produced.replace("Z", "+00:00")).timestamp()
                    if produced else os.path.getmtime(path))
        if 0 <= time.time() - observed < 1:
            time.sleep(0.002)
            if produced:
                contract["produced_at"] = datetime.now(timezone.utc).isoformat()
                with open(path, "w") as handle:
                    json.dump(contract, handle)
            else:
                os.utime(path, None)
    return task_id


def _contract_after_verdict(q, review, tmp_path, monkeypatch, *, task_id=ROOT,
                            recommended=1):
    """Publish a contract after the durable review verdict for fresh cases."""
    verdict_at = q.get_exec_state(review)["result_posted_at"]
    time.sleep(0.002)
    _contract(tmp_path, monkeypatch, recommended=recommended, task_id=task_id)
    contract = json.loads((tmp_path / "policy.json").read_text())
    assert datetime.fromisoformat(contract["produced_at"]).timestamp() > verdict_at


def test_contract_between_fix_and_review_verdict_keeps_baseline(
        q, tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    latest = "fix-rebase-376-r1"
    q.enqueue(TaskRequest(task_id=latest, task_type="implement",
                          description="fix", branch="canary-branch",
                          context={"prev_task_id": ROOT, "fix_round": 1}))
    q.submit_result(latest, TaskResult(latest, "completed", "fix done"))
    review = _review(q, root=latest, fix_round=1)
    fix_at = q.get_exec_state(latest)["result_posted_at"]
    verdict_at = q.get_exec_state(review)["result_posted_at"]
    assert fix_at <= verdict_at
    _contract(tmp_path, monkeypatch, task_id=latest)
    path = tmp_path / "policy.json"
    contract = json.loads(path.read_text())
    contract["produced_at"] = datetime.fromtimestamp(
        (fix_at + verdict_at) / 2, timezone.utc).isoformat()
    path.write_text(json.dumps(contract))
    assert _run(q, review) is None
    row = q.get_tokenomics_shadow_receipt(ROOT)
    assert row["canary_applied"] == 0
    assert row["canary_reason"] == "switch_on:pending_reresolve"
    assert json.loads(row["canary_counterfactual"])["stale_reason"] == "contract_predates_latest_result"


def test_contract_after_review_verdict_narrows(q, tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    review = _review(q)
    _contract_after_verdict(q, review, tmp_path, monkeypatch)
    assert _run(q, review) is None
    assert q.get_tokenomics_shadow_receipt(ROOT)["canary_applied"] == 1


def test_switch_off_records_fresh_counterfactual_without_holding(
        q, tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.delenv(ROUNDS_CAP_ENV, raising=False)
    review = _review(q)
    _contract_after_verdict(q, review, tmp_path, monkeypatch)
    assert _run(q, review) is not None
    row = q.get_tokenomics_shadow_receipt(ROOT)
    assert row["canary_applied"] == 0
    assert row["canary_reason"] == "switch_off:cap_not_reached"
    assert json.loads(row["canary_counterfactual"])["would_fire"] is True
    assert json.loads(row["canary_counterfactual"])["counterfactual_cap"] == 1


def _run(q, review_id, comments=None):
    return auto_enqueue_fix(q, review_id, pr_state_fn=lambda _: "open",
                            comment_fn=lambda pr, body: (comments.append(body)
                                                          if comments is not None else None),
                            already_announced_fn=lambda *a, **kw: False,
                            repo="owner/repo")


@pytest.mark.parametrize("decision_for_latest,produced_after_result,expected_reason", [
    (False, False, "contract_predates_latest_fix"),
    (True, False, "contract_predates_latest_fix"),
    (True, True, "cap_not_reached"),
])
def test_latest_fix_requires_its_own_post_result_decision(
        q, tmp_path, monkeypatch, decision_for_latest,
        produced_after_result, expected_reason):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    latest = "fix-latest"
    q.enqueue(TaskRequest(task_id=latest, task_type="implement",
                          description="fix the review", branch="canary-branch",
                          context={"prev_task_id": ROOT, "fix_round": 1}))
    if not produced_after_result:
        _contract(tmp_path, monkeypatch, task_id=latest if decision_for_latest else ROOT)
    q.submit_result(latest, TaskResult(latest, "completed", "fix completed"))
    if produced_after_result:
        _contract(tmp_path, monkeypatch, task_id=latest)
    review = _review(q, root=latest, refresh_contract=False)
    if produced_after_result:
        _contract_after_verdict(q, review, tmp_path, monkeypatch, task_id=latest)
    tasks = {task.task_id: task for task in q.list_tasks()}
    cap, citation, reason = _canary_round_cap(tasks, tasks[review], 3, q)
    assert reason == expected_reason
    assert cap == (1 if produced_after_result else 3)
    assert (citation or {}).get("cited_task_id") == (latest if decision_for_latest else ROOT)
    if not produced_after_result:
        assert _run(q, review) is None
        row = q.get_tokenomics_shadow_receipt(ROOT)
        assert row["canary_reason"] == "switch_on:pending_reresolve"
        assert json.loads(row["canary_counterfactual"])["stale_reason"] == expected_reason


def test_kill_switch_off_preserves_fix_and_shadow_receipt(q, tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.delenv(ROUNDS_CAP_ENV, raising=False)
    _contract(tmp_path, monkeypatch)
    review = _review(q)
    assert _run(q, review) is not None
    assert q.get_tokenomics_shadow_receipt(ROOT)["canary_applied"] == 0


def test_narrow_cap_holds_fix_with_cea_receipt(q, tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    _contract(tmp_path, monkeypatch)
    review = _review(q)
    comments = []
    assert _run(q, review, comments) is None
    assert _run(q, review, comments) is None  # result replay must reuse the hold
    assert len(comments) == 1
    assert "human needs to decide" in comments[0]
    assert not any(t.task_id.startswith("fix-") for t in q.list_tasks())
    row = q.get_tokenomics_shadow_receipt(ROOT)
    assert row["canary_applied"] == 1
    assert json.loads(row["actual_execution_json"])["shadow_rounds_vs_cap"] == {
        "recommended": 1, "actual_cap": 1,
    }
    assert json.loads(row["canary_counterfactual"]) == {
        "baseline_cap": 3, "effective_cap": 1, "round": 2,
        "would_have_enqueued_fix": True,
    }
    assert row["canary_cea_receipt_id"]
    with sqlite3.connect(q._db_path) as conn:
        receipt = json.loads(conn.execute(
            "SELECT receipt_json FROM authorization_receipts WHERE receipt_id=? ORDER BY seq DESC LIMIT 1",
            (row["canary_cea_receipt_id"],)).fetchone()[0])
        assert receipt["decision"] == "HUMAN_GATE"
        assert receipt["reason"]["code"] == "HUMAN_GATE_PENDING"
        assert receipt["human_gate_state"] == "PENDING"
        assert receipt["state"] == "HELD"
        assert conn.execute(
            "SELECT count(*) FROM authorization_receipts WHERE receipt_id=?",
            (row["canary_cea_receipt_id"],)).fetchone()[0] == 2


@pytest.mark.parametrize("drift", ["time", "contract_missing"])
def test_held_fix_stays_held_on_replay_when_evidence_drifts(
        q, tmp_path, monkeypatch, drift):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    review = _review(q)
    _contract_after_verdict(q, review, tmp_path, monkeypatch)
    assert _run(q, review) is None
    held = q.get_tokenomics_shadow_receipt(ROOT)
    assert held["canary_applied"] == 1
    if drift == "time":
        class FutureDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(hours=25)

        monkeypatch.setattr("agent_crew.pipeline.datetime", FutureDateTime)
    else:
        monkeypatch.delenv("AGENT_CREW_TOKENOMICS_POLICY_PATH")
    assert _run(q, review) is None
    assert not any(t.task_id == f"fix-{review}-r2" for t in q.list_tasks())
    replayed = q.get_tokenomics_shadow_receipt(ROOT)
    assert replayed["canary_applied"] == 1
    assert replayed["canary_cea_receipt_id"] == held["canary_cea_receipt_id"]
    assert replayed["canary_reason"] == "round_cap_reached"


@pytest.mark.parametrize("project,expected_reason", [
    ("agent_crew", "cap_not_reached"),
    ("other_project", "not_pinned"),
])
def test_project_pin_matches_only_its_lineage(tmp_path, monkeypatch, project, expected_reason):
    (tmp_path / project).mkdir()
    queue = TaskQueue(str(tmp_path / project / "tasks.db"))
    queue.enqueue(TaskRequest(task_id=ROOT, task_type="implement",
                              description="root", branch="canary-branch"))
    queue.submit_result(ROOT, TaskResult(ROOT, "completed", "root completed"))
    monkeypatch.setenv(CANARY_ENV, "project:agent_crew")
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    _contract(tmp_path, monkeypatch, recommended=1)
    review = _review(queue, fix_round=0)
    tasks_by_id = {task.task_id: task for task in queue.list_tasks()}
    _, _, reason = _canary_round_cap(tasks_by_id, tasks_by_id[review], 3, queue)
    assert reason == expected_reason
    assert _run(queue, review) is not None
    if project == "agent_crew":
        assert queue.get_tokenomics_shadow_receipt(ROOT)["canary_reason"] == expected_reason


def test_project_pin_applies_narrow_fresh_cap(tmp_path, monkeypatch):
    (tmp_path / "agent_crew").mkdir()
    queue = TaskQueue(str(tmp_path / "agent_crew" / "tasks.db"))
    queue.enqueue(TaskRequest(task_id=ROOT, task_type="implement",
                              description="root", branch="canary-branch"))
    queue.submit_result(ROOT, TaskResult(ROOT, "completed", "root completed"))
    monkeypatch.setenv(CANARY_ENV, "project:agent_crew")
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    _contract(tmp_path, monkeypatch, recommended=1, produced=False)
    review = _review(queue)
    assert _run(queue, review) is None
    assert queue.get_tokenomics_shadow_receipt(ROOT)["canary_applied"] == 1


def test_project_pin_uses_server_project_for_legacy_review(q, tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, "project:agent_crew")
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    _contract(tmp_path, monkeypatch, recommended=1, produced=False)
    review = _review(q, fix_round=0)
    with sqlite3.connect(q._db_path) as conn:
        conn.execute("UPDATE tasks SET project='' WHERE task_id=?", (review,))
    task = next(task for task in q.list_tasks() if task.task_id == review)
    from agent_crew.pipeline import _successor_project
    assert _successor_project(q, task) != "agent_crew"
    assert _successor_project(q, task, "agent_crew") == "agent_crew"

    assert auto_enqueue_fix(q, review, server_project="agent_crew",
                            pr_state_fn=lambda _: "open",
                            already_announced_fn=lambda *a, **kw: False,
                            repo="owner/repo") is not None
    assert q.get_tokenomics_shadow_receipt(ROOT)["canary_reason"] == "cap_not_reached"


def test_project_pin_uses_server_project_for_legacy_approved_review(q, tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, "project:agent_crew")
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    _contract(tmp_path, monkeypatch, recommended=1, produced=False)
    review = _review(q, fix_round=0, verdict="approve")
    with sqlite3.connect(q._db_path) as conn:
        conn.execute("UPDATE tasks SET project='' WHERE task_id=?", (review,))

    test_id = auto_enqueue_test(q, review, server_project="agent_crew",
                                pr_state_fn=lambda _: "open")
    assert test_id is not None
    assert q.get_tokenomics_shadow_receipt(ROOT)["canary_reason"] == "cap_not_reached"
    assert next(task for task in q.list_tasks() if task.task_id == test_id).project == "agent_crew"


def test_tier3_resume_uses_server_project_for_legacy_approved_review(q, tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, "project:agent_crew")
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    _contract(tmp_path, monkeypatch, recommended=1, produced=False)
    review = _review(q, fix_round=0, verdict="approve")
    with sqlite3.connect(q._db_path) as conn:
        conn.execute("UPDATE tasks SET project='' WHERE task_id=?", (review,))
    gate_id = f"risk-tier3-test-{review}"
    q.create_gate(GateRequest(gate_id, "approval", "approve test"))
    monkeypatch.setattr("agent_crew.pipeline.pr_is_actionable",
                        lambda *args, **kwargs: (True, "open"))
    with TestClient(create_app(q._db_path, project="agent_crew",
                               watchdog_disabled=True, anomaly_disabled=True)) as client:
        response = client.post(f"/gates/{gate_id}/resolve", json={"status": "approved"})
    assert response.status_code == 200
    test_id = f"test-{review}"
    assert q.get_tokenomics_shadow_receipt(ROOT)["canary_reason"] == "cap_not_reached"
    assert next(task for task in q.list_tasks() if task.task_id == test_id).project == "agent_crew"


def test_tier3_resume_passes_server_project_to_review_successor(q):
    q.submit_result(ROOT, TaskResult(task_id=ROOT, status="completed", summary="done"))
    with sqlite3.connect(q._db_path) as conn:
        conn.execute("UPDATE tasks SET project='' WHERE task_id=?", (ROOT,))
    gate_id = f"risk-tier3-{ROOT}"
    q.create_gate(GateRequest(gate_id, "approval", "approve review"))
    q.resolve_gate(gate_id, approved=True)

    review_id = resume_tier3_gate(q, gate_id, server_project="agent_crew",
                                  pr_state_fn=lambda _: "open")
    assert review_id is not None
    assert next(task for task in q.list_tasks() if task.task_id == review_id).project == "agent_crew"


@pytest.mark.parametrize("produced,age,mtime_age,expected_reason", [
    (False, 0, 0, "cap_not_reached"),
    (False, 0, 2, "contract_missing_or_stale"),
    (True, 2, 0, "contract_missing_or_stale"),
])
def test_contract_mtime_only_when_produced_at_absent(q, tmp_path, monkeypatch,
                                                      produced, age, mtime_age, expected_reason):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    _contract(tmp_path, monkeypatch, recommended=1, age=age,
              produced=produced, mtime_age=mtime_age)
    review = _review(q, fix_round=0)
    assert _run(q, review) is not None
    assert q.get_tokenomics_shadow_receipt(ROOT)["canary_reason"] == expected_reason


@pytest.mark.parametrize("recommended,age,expected_reason", [
    (3, 0, "recommendation_does_not_narrow"),
    (1, 2, "contract_missing_or_stale"),
    (0, 0, "invalid_recommendation"),
])
def test_no_narrowing_uses_baseline(q, tmp_path, monkeypatch,
                                    recommended, age, expected_reason):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    _contract(tmp_path, monkeypatch, recommended, age=age)
    review = _review(q)
    assert _run(q, review) is not None
    row = q.get_tokenomics_shadow_receipt(ROOT)
    assert row["canary_applied"] == 0
    assert row["canary_reason"] == expected_reason


def test_other_lineage_uses_baseline(q, tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    _contract(tmp_path, monkeypatch)
    review = _review(q, root="unrelated")
    assert _run(q, review) is not None
    assert q.get_tokenomics_shadow_receipt(ROOT)["canary_applied"] is None


def test_existing_identical_sha_canary_receipt_is_preserved(q, tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    _contract(tmp_path, monkeypatch)
    review = _review(q, fix_round=0)
    q.record_tokenomics_canary_receipt(
        review, decision_source="quota_core_contract",
        recommendation={"kind": "suppress_identical_sha_rereview"},
        applied=True, counterfactual="review skipped",
        reason="standing_request_changes_on_identical_sha")
    assert _run(q, review) is not None
    assert q.get_tokenomics_shadow_receipt(review)["canary_reason"] == (
        "standing_request_changes_on_identical_sha")
    assert q.get_tokenomics_shadow_receipt(ROOT)["canary_reason"] == "cap_not_reached"


def test_cap_not_reached_keeps_review_then_test(q, tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    _contract(tmp_path, monkeypatch)
    review = _review(q, fix_round=0)
    fix = _run(q, review)
    assert fix is not None
    assert q.get_tokenomics_shadow_receipt(ROOT)["canary_reason"] == "cap_not_reached"
    approved = _review(q, task_id="review-canary-approved", fix_round=1,
                       verdict="approve")
    assert _run(q, approved) is None
    test = auto_enqueue_test(q, approved, pr_state_fn=lambda _: "open")
    assert test is not None
    assert {t.task_id: t.task_type for t in q.list_tasks()}[test] == "test"
    assert q.get_tokenomics_shadow_receipt(ROOT)["canary_applied"] == 0
    assert q.get_tokenomics_shadow_receipt(ROOT)["canary_reason"] == "cap_not_reached"


def test_contract_produced_after_review_decision_cannot_narrow_retroactively(
        q, tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    review = _review(q)
    decision_at = q.get_exec_state(review)["events"][-1]["at"]
    path = tmp_path / "future.json"
    path.write_text(json.dumps({
        "contract_version": "1.0", "mode": "shadow",
        "produced_at": datetime.fromtimestamp(decision_at + 60, timezone.utc).isoformat(),
        "decisions": [{"task_id": ROOT, "recommended_max_review_fix_rounds": 1}],
    }))
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(path))
    assert _run(q, review) is not None
    row = q.get_tokenomics_shadow_receipt(ROOT)
    assert row["canary_applied"] == 0
    assert row["canary_reason"] == "contract_missing_or_stale"


def test_pre_verdict_lineage_receipt_cannot_narrow_with_future_contract(
        q, tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    review = _review(q)
    decision_at = q.get_exec_state(review)["events"][-1]["at"]
    result_at = q.get_exec_state(ROOT)["result_posted_at"]
    produced_at = (result_at + decision_at) / 2
    with sqlite3.connect(q._db_path) as conn:
        conn.execute(
            """UPDATE tokenomics_shadow_receipts
               SET shadow_decision_source='quota_core_contract',
                   shadow_recommendation_json=?, shadow_resolved_at=?,
                   shadow_contract_sha='earlier-contract'
               WHERE task_id=?""",
            (json.dumps({"produced_at": datetime.fromtimestamp(
                produced_at, timezone.utc).isoformat(),
                "recommendation": {"recommended_max_review_fix_rounds": 1}}),
             produced_at, ROOT))
    path = tmp_path / "future.json"
    path.write_text(json.dumps({
        "contract_version": "1.0", "mode": "shadow",
        "produced_at": datetime.fromtimestamp(decision_at + 60, timezone.utc).isoformat(),
        "decisions": [{"task_id": review, "recommended_max_review_fix_rounds": 2}],
    }))
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(path))
    assert _run(q, review) is None
    row = q.get_tokenomics_shadow_receipt(ROOT)
    assert row["canary_reason"] == "switch_on:pending_reresolve"
    assert json.loads(row["canary_counterfactual"])["stale_reason"] == "contract_predates_latest_result"
    cited = json.loads(row["canary_recommendation_json"])
    assert cited["cited_task_id"] == ROOT
    assert cited["contract_sha"] == "earlier-contract"
    assert cited["recommended_max_review_fix_rounds"] == 1
    assert cited["produced_at"] == datetime.fromtimestamp(
        produced_at, timezone.utc).isoformat()


def test_pre_verdict_mtime_receipt_cannot_narrow_with_future_contract(
        q, tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    _contract(tmp_path, monkeypatch, recommended=1, produced=False)
    path = tmp_path / "policy.json"
    # Model a post-result contract re-emit captured in the durable receipt.
    observed_mtime = datetime.now(timezone.utc).timestamp()
    os.utime(path, (observed_mtime, observed_mtime))
    q._refresh_shadow_after_commit(ROOT)
    stored = q.get_tokenomics_shadow_receipt(ROOT)
    assert json.loads(stored["shadow_recommendation_json"])["produced_at"] == (
        datetime.fromtimestamp(observed_mtime, timezone.utc).isoformat())

    review = _review(q, refresh_contract=False)
    decision_at = q.get_exec_state(review)["events"][-1]["at"]
    # The re-emitted live file is newer than the review decision and must not
    # be used; only the already-stored implementation receipt is eligible.
    future_mtime = decision_at + 60
    os.utime(path, (future_mtime, future_mtime))
    assert _run(q, review) is None
    row = q.get_tokenomics_shadow_receipt(ROOT)
    assert row["canary_reason"] == "switch_on:pending_reresolve"
    assert json.loads(row["canary_counterfactual"])["stale_reason"] == "contract_predates_latest_result"
    citation = json.loads(row["canary_recommendation_json"])
    assert citation["decision_source"] == "quota_core_contract"
    assert citation["cited_task_id"] == ROOT
    assert citation["produced_at"] == datetime.fromtimestamp(
        observed_mtime, timezone.utc).isoformat()


def test_recently_read_stale_contract_receipt_uses_baseline(q, tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    review = _review(q)
    decision_at = q.get_exec_state(review)["events"][-1]["at"]
    with sqlite3.connect(q._db_path) as conn:
        conn.execute(
            """UPDATE tokenomics_shadow_receipts
               SET shadow_decision_source='quota_core_contract',
                   shadow_recommendation_json=?, shadow_resolved_at=?
               WHERE task_id=?""",
            (json.dumps({"produced_at": datetime.fromtimestamp(
                decision_at - timedelta(days=10).total_seconds(), timezone.utc).isoformat(),
                "recommendation": {"recommended_max_review_fix_rounds": 1}}),
             decision_at - 1, ROOT))
    monkeypatch.delenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", raising=False)
    assert _run(q, review) is not None
    row = q.get_tokenomics_shadow_receipt(ROOT)
    assert row["canary_applied"] == 0
    assert row["canary_reason"] == "contract_missing_or_stale"
    assert json.loads(row["canary_recommendation_json"])[
        "recommended_max_review_fix_rounds"] == 1


def test_untimed_legacy_contract_has_no_citation_or_recommended_value(
        q, tmp_path, monkeypatch):
    monkeypatch.setenv(CANARY_ENV, ROOT)
    monkeypatch.setenv(ROUNDS_CAP_ENV, "1")
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"recommendation": {
        "recommended_max_review_fix_rounds": 1}}))
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(path))
    review = _review(q)
    # Pre-upgrade receipts carried only the recommendation, with no contract
    # production timestamp even when read at review completion.
    with sqlite3.connect(q._db_path) as conn:
        conn.execute(
            """UPDATE tokenomics_shadow_receipts
               SET shadow_decision_source='quota_core_contract',
                   shadow_recommendation_json=?, shadow_resolved_at=?
               WHERE task_id=?""",
            (json.dumps({"recommended_max_review_fix_rounds": 1}),
             q.get_exec_state(review)["events"][-1]["at"] - 1, ROOT))
    fix = _run(q, review)
    assert fix is not None
    citation = {t.task_id: t for t in q.list_tasks()}[fix].context["tokenomics_shadow"]
    assert citation["decision_source"] == "baseline"
    assert citation["recommended_max_review_fix_rounds"] is None
    row = q.get_tokenomics_shadow_receipt(ROOT)
    assert row["canary_applied"] == 0
    assert row["canary_reason"] == "contract_missing_or_stale"
