"""Project-scoped predecessor pins in hybrid retrieval (#740)."""
import sqlite3

from agent_crew.memory_hybrid import HybridMemoryStorage, _bytes
from agent_crew.memory_runtime import MemoryRecord, MemoryScope


def _put(store, key, *, project="agent_crew", task_id="", layer="decision"):
    store.put(MemoryRecord(layer, key, {"task_id": task_id, "text": "signal " + key},
                           MemoryScope(project=project, task_id=task_id)))


def test_pin_reaches_predecessor_across_task_scope_mismatch(tmp_path):
    store = HybridMemoryStorage(str(tmp_path / "memory.db"))
    _put(store, "task:review-1:decision", task_id="review-1")
    scope = MemoryScope(project="agent_crew", task_id="fix-1")
    baseline = store.retrieve_ranked(scope, "signal", "implementer", 10, 8000)
    assert not baseline["middle"]
    result = store.retrieve_ranked(scope, "signal", "implementer", 10, 8000,
                                   predecessor_task_ids=("review-1",))
    assert result["middle"][0]["key"] == "task:review-1:decision"


def test_pin_reserves_one_slot_ahead_of_crowded_middle(tmp_path):
    store = HybridMemoryStorage(str(tmp_path / "memory.db"))
    _put(store, "task:review-1:decision", task_id="review-1")
    for index in range(110):
        _put(store, f"other:{index:03}")
    result = store.retrieve_ranked(MemoryScope(project="agent_crew", task_id="fix-1"),
                                   "signal", "implementer", 1, 8000,
                                   predecessor_task_ids=("review-1",))
    assert [row["key"] for row in result["middle"]] == ["task:review-1:decision"]


def test_outcome_pin_reserves_bytes_before_ranked_rows(tmp_path):
    store = HybridMemoryStorage(str(tmp_path / "memory.db"))
    _put(store, "task_outcome:agent_crew:review-1", task_id="",
         layer="episodic")
    _put(store, "local:signal")
    scope = MemoryScope(project="agent_crew", task_id="fix-1")
    full = store.retrieve_ranked(scope, "signal", "implementer", 10, 8000,
                                 predecessor_task_ids=("review-1",))
    pin = full["middle"][0]
    assert pin["key"] == "task_outcome:agent_crew:review-1"
    result = store.retrieve_ranked(scope, "signal", "implementer", 10, _bytes(pin),
                                   predecessor_task_ids=("review-1",))
    assert [row["key"] for row in result["middle"]] == [pin["key"]]


def test_pin_never_crosses_project_and_default_is_unchanged(tmp_path):
    store = HybridMemoryStorage(str(tmp_path / "memory.db"))
    _put(store, "task:foreign:decision", project="other", task_id="foreign")
    _put(store, "local:signal")
    scope = MemoryScope(project="agent_crew", task_id="fix-1")
    baseline = store.retrieve_ranked(scope, "signal", "implementer", 10, 8000)
    result = store.retrieve_ranked(scope, "signal", "implementer", 10, 8000,
                                   predecessor_task_ids=("foreign",))
    assert [row["key"] for row in result["middle"]] == [row["key"] for row in baseline["middle"]]


def test_pin_never_crosses_fleet(tmp_path):
    store = HybridMemoryStorage(str(tmp_path / "memory.db"))
    store.put(MemoryRecord("decision", "task:review-1:decision",
                           {"task_id": "review-1"},
                           MemoryScope(project="agent_crew", fleet="other",
                                       task_id="review-1")))
    result = store.retrieve_ranked(MemoryScope(project="agent_crew", fleet="fleet",
                                               task_id="fix-1"),
                                   "signal", "implementer", 10, 8000,
                                   predecessor_task_ids=("review-1",))
    assert result["middle"] == []


def test_retired_pin_is_removed_at_render_time(tmp_path, monkeypatch):
    store = HybridMemoryStorage(str(tmp_path / "memory.db"))
    _put(store, "task:review-1:decision", task_id="review-1")
    original_rank = store._rank_middle
    def retire_then_rank(*args):
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE adr001_memory SET invalidated_at=1 "
                       "WHERE key='task:review-1:decision'")
            db.commit()
        return original_rank(*args)
    monkeypatch.setattr(store, "_rank_middle", retire_then_rank)
    result = store.retrieve_ranked(MemoryScope(project="agent_crew", task_id="fix-1"),
                                   "signal", "implementer", 10, 8000,
                                   predecessor_task_ids=("review-1",))
    assert "task:review-1:decision" not in [row["key"] for row in result["middle"]]
