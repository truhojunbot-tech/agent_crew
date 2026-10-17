"""#342 E3: late quota decisions update shadow evidence only."""

import json
import sqlite3
import time
from datetime import datetime, timezone

from agent_crew.pipeline import review_fix_max_rounds
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue, _CEA_SYSTEM_SUCCESSOR_PROVENANCE


def _emit(path, decisions):
    path.write_text(json.dumps({"contract_version": "1.0", "mode": "shadow",
                                "produced_at": datetime.now(timezone.utc).isoformat(),
                                "decisions": decisions}))


def _preserved(queue, task_id):
    with sqlite3.connect(queue._db_path) as conn:
        conn.execute("UPDATE tokenomics_shadow_receipts SET "
                     "canary_decision_source='baseline', canary_applied=0, "
                     "canary_reason='unchanged' WHERE task_id=?", (task_id,))
    row = queue.get_tokenomics_shadow_receipt(task_id)
    return tuple(row[key] for key in ("decision_source", "recommendation_json",
                                      "canary_decision_source", "canary_applied",
                                      "canary_reason"))


def _assert_preserved(queue, task_id, before):
    row = queue.get_tokenomics_shadow_receipt(task_id)
    assert tuple(row[key] for key in ("decision_source", "recommendation_json",
                                        "canary_decision_source", "canary_applied",
                                        "canary_reason")) == before


def test_late_test_decision_reresolves_after_contract_reemit(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    _emit(policy, [])
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("test-e3", "test", "run suite", branch="agent/e3"))
    before = _preserved(queue, "test-e3")
    _emit(policy, [{"task_id": "test-e3", "recommended_provider_tier": "preserve_current"}])
    assert queue.reresolve_tokenomics_shadow_receipts() == 1
    row = queue.get_tokenomics_shadow_receipt("test-e3")
    assert row["shadow_decision_source"] == "quota_core_contract"
    assert json.loads(row["shadow_recommendation_json"])["recommendation"]["task_id"] == "test-e3"
    _assert_preserved(queue, "test-e3", before)


def test_late_review_decision_reresolves_after_contract_reemit(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    _emit(policy, [])
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("review-e3", "review", "review code", branch="agent/e3",
                              context={"pr_number": 342, "reviewed_sha": "a" * 40}))
    before = _preserved(queue, "review-e3")
    _emit(policy, [{"task_id": "review-e3", "recommended_max_review_fix_rounds": 2}])
    assert queue.reresolve_tokenomics_shadow_receipts() == 1
    row = queue.get_tokenomics_shadow_receipt("review-e3")
    assert row["shadow_decision_source"] == "quota_core_contract"
    assert json.loads(row["shadow_recommendation_json"])["recommendation"]["task_id"] == "review-e3"
    _assert_preserved(queue, "review-e3", before)


def test_missing_after_settle_window_stays_unknown(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    _emit(policy, [])
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("test-expired", "test", "run suite", branch="agent/e3"))
    before = _preserved(queue, "test-expired")
    with sqlite3.connect(queue._db_path) as conn:
        conn.execute("UPDATE tokenomics_shadow_receipts SET created_at=? WHERE task_id=?",
                     (time.time() - 7 * 3600, "test-expired"))
    assert queue.reresolve_tokenomics_shadow_receipts() == 1
    first = queue.get_tokenomics_shadow_receipt("test-expired")
    assert first["shadow_reason"] == "UNKNOWN_NOT_IN_CONTRACT"
    _emit(policy, [{"task_id": "test-expired", "recommended_provider_tier": "preserve_current"}])
    assert queue.reresolve_tokenomics_shadow_receipts() == 0
    assert queue.get_tokenomics_shadow_receipt("test-expired")["shadow_reason"] == "UNKNOWN_NOT_IN_CONTRACT"
    _assert_preserved(queue, "test-expired", before)


def test_decision_present_at_first_sweep_after_deadline_is_recorded(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    _emit(policy, [])
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("test-late-sweep", "test", "run suite", branch="agent/e3"))
    with sqlite3.connect(queue._db_path) as conn:
        conn.execute("UPDATE tokenomics_shadow_receipts SET created_at=? WHERE task_id=?",
                     (time.time() - 7 * 3600, "test-late-sweep"))
    _emit(policy, [{"task_id": "test-late-sweep"}])
    assert queue.reresolve_tokenomics_shadow_receipts() == 1
    assert queue.get_tokenomics_shadow_receipt("test-late-sweep")["shadow_decision_source"] == "quota_core_contract"


def test_successor_cites_nearest_predecessor_without_changing_execution(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    _emit(policy, [{"task_id": "impl-e3", "recommended_max_review_fix_rounds": 1}])
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    monkeypatch.delenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", raising=False)
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("impl-e3", "implement", "implement", branch="agent/e3"))
    queue.enqueue(TaskRequest("test-e3-successor", "test", "test", branch="agent/e3",
                              context={"prev_task_id": "impl-e3", "agent_override": "gemini"}),
                  _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
    before = _preserved(queue, "test-e3-successor")
    task_before = queue.get_task("test-e3-successor")
    cap_before = review_fix_max_rounds()
    row = queue.get_tokenomics_shadow_receipt("test-e3-successor")
    assert row["shadow_decision_source"] == "quota_core_contract"
    cited = json.loads(row["shadow_recommendation_json"])
    assert cited["cited_task_id"] == "impl-e3"
    assert cited["recommendation"]["recommended_max_review_fix_rounds"] == 1
    assert queue.get_task("test-e3-successor").context["agent_override"] == task_before.context["agent_override"]
    assert review_fix_max_rounds() == cap_before == 3
    _assert_preserved(queue, "test-e3-successor", before)


def test_successor_walks_past_a_parent_without_own_decision(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    _emit(policy, [{"task_id": "impl-root", "recommended_max_review_fix_rounds": 2}])
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("impl-root", "implement", "implement", branch="agent/e3"))
    queue.enqueue(TaskRequest("review-middle", "review", "review", branch="agent/e3",
                              context={"prev_task_id": "impl-root", "pr_number": 342,
                                       "reviewed_sha": "a" * 40}),
                  _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
    queue.enqueue(TaskRequest("test-leaf", "test", "test", branch="agent/e3",
                              context={"prev_task_id": "review-middle"}),
                  _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
    row = queue.get_tokenomics_shadow_receipt("test-leaf")
    assert json.loads(row["shadow_recommendation_json"])["cited_task_id"] == "impl-root"


def test_reresolve_batch_is_bounded_and_keeps_admission_and_canary(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    _emit(policy, [])
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    for task_id in ("test-one", "test-two"):
        queue.enqueue(TaskRequest(task_id, "test", "run", branch=f"agent/{task_id}"))
    before = {task_id: _preserved(queue, task_id) for task_id in ("test-one", "test-two")}
    _emit(policy, [{"task_id": task_id} for task_id in ("test-one", "test-two")])
    assert queue.reresolve_tokenomics_shadow_receipts(limit=1) == 1
    assert queue.reresolve_tokenomics_shadow_receipts(limit=1) == 1
    assert queue.reresolve_tokenomics_shadow_receipts(limit=1) == 0
    for task_id, old in before.items():
        assert queue.get_tokenomics_shadow_receipt(task_id)["shadow_decision_source"] == "quota_core_contract"
        _assert_preserved(queue, task_id, old)
