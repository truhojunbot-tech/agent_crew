"""Project-scoped predecessor pins in hybrid retrieval (#740)."""
import sqlite3
import time

from fastapi.testclient import TestClient

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


def test_six_predecessor_decisions_precede_outcomes(tmp_path):
    store = HybridMemoryStorage(str(tmp_path / "memory.db"))
    predecessors = tuple(f"review-{i}" for i in range(6))
    for task_id in predecessors:
        _put(store, f"task:{task_id}:decision", task_id=task_id)
        _put(store, f"task_outcome:agent_crew:{task_id}", layer="episodic")
    result = store.retrieve_ranked(MemoryScope(project="agent_crew", task_id="fix"),
                                   "unrelated", "implementer", 10, 16000,
                                   predecessor_task_ids=predecessors)
    keys = [row["key"] for row in result["middle"]]
    assert keys[:6] == [f"task:{task_id}:decision" for task_id in predecessors]


def test_pin_budget_leaves_standing_overflow_slots_and_bytes(tmp_path):
    store = HybridMemoryStorage(str(tmp_path / "memory.db"))
    predecessors = tuple(f"review-{i}" for i in range(12))
    for task_id in predecessors:
        store.put(MemoryRecord("decision", f"task:{task_id}:decision",
                               {"text": "p" * 1500},
                               MemoryScope(project="agent_crew", task_id=task_id)))
    for index in range(40):
        store.put(MemoryRecord("decision", f"standing:{index}",
                               {"kind": "standing_decision", "verb": "keep",
                                "subject": str(index), "source_message_id": str(index),
                                "text": "s" * 250}, MemoryScope(project="agent_crew")))
    result = store.retrieve_ranked(MemoryScope(project="agent_crew", task_id="fix"),
                                   "unrelated", "implementer", 14, 16000,
                                   predecessor_task_ids=predecessors)
    assert sum(row["key"].startswith("standing:") for row in result["middle"]) >= 2
    assert sum(row["key"].startswith("task:review-") for row in result["middle"]) <= 5


def test_fast_lexical_only_when_effective_pin_exists_and_fallback_keeps_pin(tmp_path, monkeypatch):
    store = HybridMemoryStorage(str(tmp_path / "memory.db"))
    _put(store, "task:review-1:decision", task_id="review-1")
    _put(store, "local:signal")
    scope = MemoryScope(project="agent_crew", task_id="fix")
    original_rank = store._rank_middle
    flags = []
    def rank(candidates, query, fast_lexical=False):
        flags.append(fast_lexical)
        return original_rank(candidates, query, fast_lexical)
    monkeypatch.setattr(store, "_rank_middle", rank)
    store.retrieve_ranked(scope, "signal", "implementer", 10, 8000,
                          predecessor_task_ids=("missing",))
    store.retrieve_ranked(scope, "signal", "implementer", 10, 8000,
                          predecessor_task_ids=("review-1",))
    assert flags == [False, True]
    def fail_rank(*args, **kwargs):
        raise RuntimeError("ranking unavailable")
    monkeypatch.setattr(store, "_rank_middle", fail_rank)
    result = store.retrieve_ranked(scope, "signal", "implementer", 10, 8000,
                                   predecessor_task_ids=("review-1",))
    assert result["mode"] == "fallback"
    assert result["middle"][0]["key"] == "task:review-1:decision"


def test_http_lineage_lookup_error_degrades_to_no_pins(tmp_path, monkeypatch):
    from agent_crew import server

    path = tmp_path / "memory.db"
    store = HybridMemoryStorage(str(path))
    _put(store, "task:review-1:decision", task_id="review-1")
    monkeypatch.setenv("AGENT_CREW_MEMORY_BACKEND", "hybrid")
    monkeypatch.setenv("AGENT_CREW_MEMORY_DB", str(path))
    def broken_lineage(*args):
        raise sqlite3.OperationalError("tasks db locked")
    monkeypatch.setattr(server, "task_lineage", broken_lineage)
    with TestClient(server.create_app(str(tmp_path / "tasks.db"), project="agent_crew",
                                      watchdog_disabled=True)) as client:
        response = client.post("/memory/retrieve_ranked",
                               headers={"X-Agent-Crew-Project": "agent_crew"},
                               json={"scope": {"project": "agent_crew", "task_id": "fix"},
                                     "query": "signal", "k": 10, "byte_budget": 8000})
    assert response.status_code == 200
    assert "task:review-1:decision" not in [row["key"] for row in response.json()["middle"]]


def test_http_lineage_lookup_is_inside_timeout_and_passes_pins(tmp_path, monkeypatch):
    from agent_crew import server

    path = tmp_path / "memory.db"
    store = HybridMemoryStorage(str(path))
    _put(store, "task:review-1:decision", task_id="review-1")
    monkeypatch.setenv("AGENT_CREW_MEMORY_BACKEND", "hybrid")
    monkeypatch.setenv("AGENT_CREW_MEMORY_DB", str(path))
    monkeypatch.setattr(server, "task_lineage", lambda *args: (("review-1",), None))
    with TestClient(server.create_app(str(tmp_path / "tasks.db"), project="agent_crew",
                                      watchdog_disabled=True)) as client:
        payload = {"scope": {"project": "agent_crew", "task_id": "fix"},
                   "query": "signal", "k": 10, "byte_budget": 8000}
        response = client.post("/memory/retrieve_ranked",
                               headers={"X-Agent-Crew-Project": "agent_crew"}, json=payload)
        assert response.status_code == 200
        assert response.json()["middle"][0]["key"] == "task:review-1:decision"
        def slow_lineage(*args):
            time.sleep(.45)
            return ("review-1",), None
        monkeypatch.setattr(server, "task_lineage", slow_lineage)
        timeout = client.post("/memory/retrieve_ranked",
                              headers={"X-Agent-Crew-Project": "agent_crew"}, json=payload)
        assert timeout.status_code == 200
        assert timeout.json()["mode"] == "fallback"
