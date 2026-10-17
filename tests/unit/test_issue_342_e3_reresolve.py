"""#342 E3: late quota decisions update shadow evidence only."""

import json
import sqlite3
import time
from datetime import datetime, timezone

from agent_crew.pipeline import review_fix_max_rounds
from agent_crew.protocol import TaskRequest
from agent_crew.queue import TaskQueue, _CEA_SYSTEM_SUCCESSOR_PROVENANCE


def _emit(path, decisions, *, produced_at=None):
    path.write_text(json.dumps({"contract_version": "1.0", "mode": "shadow",
                                "produced_at": produced_at or datetime.now(timezone.utc).isoformat(),
                                "decisions": decisions}))


def _posted(queue, task_id, at=None):
    posted_at = at if at is not None else time.time() - 2
    with sqlite3.connect(queue._db_path) as conn:
        conn.execute("UPDATE tasks SET result_posted_at=? WHERE task_id=?", (posted_at, task_id))
    return posted_at


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
    _posted(queue, "test-e3")
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
    _posted(queue, "review-e3")
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
    _posted(queue, "test-late-sweep")
    with sqlite3.connect(queue._db_path) as conn:
        conn.execute("UPDATE tokenomics_shadow_receipts SET created_at=? WHERE task_id=?",
                     (time.time() - 7 * 3600, "test-late-sweep"))
    _emit(policy, [{"task_id": "test-late-sweep"}])
    assert queue.reresolve_tokenomics_shadow_receipts() == 1
    assert queue.get_tokenomics_shadow_receipt("test-late-sweep")["shadow_decision_source"] == "quota_core_contract"


def test_unavailable_contract_does_not_finalize_absence(tmp_path, monkeypatch):
    policy = tmp_path / "missing-policy.json"
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("test-unavailable", "test", "run suite", branch="agent/e3"))
    _posted(queue, "test-unavailable")
    with sqlite3.connect(queue._db_path) as conn:
        conn.execute("UPDATE tokenomics_shadow_receipts SET created_at=? WHERE task_id=?",
                     (time.time() - 7 * 3600, "test-unavailable"))
    assert queue.reresolve_tokenomics_shadow_receipts() == 0
    assert queue.get_tokenomics_shadow_receipt("test-unavailable")["shadow_reason"] != "UNKNOWN_NOT_IN_CONTRACT"
    _emit(policy, [{"task_id": "test-unavailable"}])
    assert queue.reresolve_tokenomics_shadow_receipts() == 1
    assert queue.get_tokenomics_shadow_receipt("test-unavailable")["shadow_decision_source"] == "quota_core_contract"


def test_unsupported_contract_does_not_finalize_absence(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"contract_version": "unsupported", "mode": "shadow",
                                  "decisions": []}))
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("test-unsupported", "test", "run suite", branch="agent/e3"))
    _posted(queue, "test-unsupported")
    with sqlite3.connect(queue._db_path) as conn:
        conn.execute("UPDATE tokenomics_shadow_receipts SET created_at=? WHERE task_id=?",
                     (time.time() - 7 * 3600, "test-unsupported"))
    queue.reresolve_tokenomics_shadow_receipts()
    assert queue.get_tokenomics_shadow_receipt("test-unsupported")["shadow_reason"] != "UNKNOWN_NOT_IN_CONTRACT"
    _emit(policy, [{"task_id": "test-unsupported"}])
    assert queue.reresolve_tokenomics_shadow_receipts() == 1
    assert queue.get_tokenomics_shadow_receipt("test-unsupported")["shadow_decision_source"] == "quota_core_contract"


def test_same_second_decision_waits_for_fresh_reemit(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    _emit(policy, [])
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("test-freshness", "test", "run suite", branch="agent/e3"))
    before = _preserved(queue, "test-freshness")
    posted_at = _posted(queue, "test-freshness", time.time())
    same_second = datetime.fromtimestamp(posted_at, timezone.utc).replace(microsecond=0).isoformat()
    _emit(policy, [{"task_id": "test-freshness", "confidence": "low"}],
          produced_at=same_second)
    assert queue.reresolve_tokenomics_shadow_receipts() == 1
    pending = queue.get_tokenomics_shadow_receipt("test-freshness")
    assert pending["shadow_decision_source"] in (None, "baseline")
    assert pending["shadow_reason"] == "decision_not_after_result"
    _assert_preserved(queue, "test-freshness", before)

    fresh_at = datetime.fromtimestamp(posted_at + 2, timezone.utc).isoformat()
    _emit(policy, [{"task_id": "test-freshness", "confidence": "medium"}],
          produced_at=fresh_at)
    assert queue.reresolve_tokenomics_shadow_receipts() == 1
    resolved = queue.get_tokenomics_shadow_receipt("test-freshness")
    assert resolved["shadow_decision_source"] == "quota_core_contract"
    assert json.loads(resolved["shadow_recommendation_json"])["recommendation"]["confidence"] == "medium"
    _assert_preserved(queue, "test-freshness", before)


def test_running_task_cannot_accept_contract_decision(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    _emit(policy, [])
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("test-running", "test", "run suite", branch="agent/e3"))
    _emit(policy, [{"task_id": "test-running"}])
    assert queue.reresolve_tokenomics_shadow_receipts() == 1
    row = queue.get_tokenomics_shadow_receipt("test-running")
    assert row["shadow_decision_source"] in (None, "baseline")
    assert row["shadow_reason"] == "decision_not_after_result"


def test_successor_waits_for_predecessor_post_outcome_decision(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    _emit(policy, [])
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("impl-freshness", "implement", "implement", branch="agent/e3"))
    posted_at = _posted(queue, "impl-freshness", time.time())
    same_second = datetime.fromtimestamp(posted_at, timezone.utc).replace(microsecond=0).isoformat()
    _emit(policy, [{"task_id": "impl-freshness", "confidence": "low"}],
          produced_at=same_second)
    assert queue.reresolve_tokenomics_shadow_receipts() == 1
    assert queue.get_tokenomics_shadow_receipt("impl-freshness")["shadow_decision_source"] == "baseline"

    queue.enqueue(TaskRequest("review-freshness", "review", "review", branch="agent/e3",
                              context={"prev_task_id": "impl-freshness", "pr_number": 342,
                                       "reviewed_sha": "a" * 40}),
                  _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
    pending = queue.get_tokenomics_shadow_receipt("review-freshness")
    assert pending["shadow_decision_source"] in (None, "baseline")
    assert pending["shadow_reason"] == "decision_not_after_result"

    _emit(policy, [{"task_id": "impl-freshness", "confidence": "medium"}],
          produced_at=datetime.fromtimestamp(posted_at + 2, timezone.utc).isoformat())
    assert queue.reresolve_tokenomics_shadow_receipts() == 2
    resolved = queue.get_tokenomics_shadow_receipt("review-freshness")
    assert resolved["shadow_decision_source"] == "quota_core_contract"
    cited = json.loads(resolved["shadow_recommendation_json"])
    assert cited["cited_task_id"] == "impl-freshness"
    assert cited["recommendation"]["confidence"] == "medium"


def test_successor_rejects_stored_predecessor_decision_from_before_result(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    _emit(policy, [])
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("impl-stored", "implement", "implement", branch="agent/e3"))
    posted_at = _posted(queue, "impl-stored", time.time())
    stale_at = datetime.fromtimestamp(posted_at, timezone.utc).replace(microsecond=0).isoformat()
    with sqlite3.connect(queue._db_path) as conn:
        conn.execute(
            "UPDATE tokenomics_shadow_receipts SET shadow_decision_source='quota_core_contract', "
            "shadow_recommendation_json=?, shadow_contract_sha='stored' WHERE task_id='impl-stored'",
            (json.dumps({"produced_at": stale_at,
                         "recommendation": {"task_id": "impl-stored", "confidence": "low"}}),))
    queue.enqueue(TaskRequest("review-stored", "review", "review", branch="agent/e3",
                              context={"prev_task_id": "impl-stored", "pr_number": 342,
                                       "reviewed_sha": "a" * 40}),
                  _successor_provenance=_CEA_SYSTEM_SUCCESSOR_PROVENANCE)
    pending = queue.get_tokenomics_shadow_receipt("review-stored")
    assert pending["shadow_decision_source"] == "baseline"
    assert pending["shadow_reason"] == "decision_not_after_result"
    assert pending["shadow_recommendation_json"] is None

    fresh_at = datetime.fromtimestamp(posted_at + 2, timezone.utc).isoformat()
    with sqlite3.connect(queue._db_path) as conn:
        conn.execute("UPDATE tokenomics_shadow_receipts SET shadow_recommendation_json=? "
                     "WHERE task_id='impl-stored'",
                     (json.dumps({"produced_at": fresh_at,
                                  "recommendation": {"task_id": "impl-stored",
                                                     "confidence": "medium"}}),))
    queue._refresh_shadow_after_commit("review-stored")
    resolved = queue.get_tokenomics_shadow_receipt("review-stored")
    assert resolved["shadow_decision_source"] == "quota_core_contract"
    assert json.loads(resolved["shadow_recommendation_json"])["cited_task_id"] == "impl-stored"


def test_pre_outcome_decision_expires_if_never_refreshed(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    _emit(policy, [])
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("test-stale", "test", "run suite", branch="agent/e3"))
    posted_at = _posted(queue, "test-stale", time.time())
    with sqlite3.connect(queue._db_path) as conn:
        conn.execute("UPDATE tokenomics_shadow_receipts SET created_at=? WHERE task_id=?",
                     (time.time() - 7 * 3600, "test-stale"))
    _emit(policy, [{"task_id": "test-stale"}],
          produced_at=datetime.fromtimestamp(posted_at, timezone.utc).replace(
              microsecond=0).isoformat())
    assert queue.reresolve_tokenomics_shadow_receipts() == 1
    assert queue.get_tokenomics_shadow_receipt("test-stale")["shadow_reason"] == "UNKNOWN_NOT_IN_CONTRACT"


def test_successor_cites_nearest_predecessor_without_changing_execution(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    _emit(policy, [])
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    monkeypatch.delenv("AGENT_CREW_REVIEW_FIX_MAX_ROUNDS", raising=False)
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("impl-e3", "implement", "implement", branch="agent/e3"))
    _posted(queue, "impl-e3")
    _emit(policy, [{"task_id": "impl-e3", "recommended_max_review_fix_rounds": 1}])
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
    _emit(policy, [])
    monkeypatch.setenv("AGENT_CREW_TOKENOMICS_POLICY_PATH", str(policy))
    queue = TaskQueue(str(tmp_path / "tasks.db"))
    queue.enqueue(TaskRequest("impl-root", "implement", "implement", branch="agent/e3"))
    _posted(queue, "impl-root")
    _emit(policy, [{"task_id": "impl-root", "recommended_max_review_fix_rounds": 2}])
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
        _posted(queue, task_id)
    before = {task_id: _preserved(queue, task_id) for task_id in ("test-one", "test-two")}
    _emit(policy, [{"task_id": task_id} for task_id in ("test-one", "test-two")])
    assert queue.reresolve_tokenomics_shadow_receipts(limit=1) == 1
    assert queue.reresolve_tokenomics_shadow_receipts(limit=1) == 1
    assert queue.reresolve_tokenomics_shadow_receipts(limit=1) == 0
    for task_id, old in before.items():
        assert queue.get_tokenomics_shadow_receipt(task_id)["shadow_decision_source"] == "quota_core_contract"
        _assert_preserved(queue, task_id, old)
