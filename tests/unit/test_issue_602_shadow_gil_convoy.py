"""Shadow SQLite reads remain equivalent and bounded under Python CPU contention."""

import json
import sqlite3
import statistics
import threading
import time

from agent_crew.memory_runtime import MemoryRecord, MemoryScope, SQLiteMemoryStorage


def _insert(db, layer, key, value, scope, created):
    db.execute(
        "INSERT INTO adr001_memory VALUES (?,?,?,?,?,?)",
        (layer, key, json.dumps(value), json.dumps(scope), 1, created),
    )


def test_aggregated_shadow_rows_preserve_lineage_pr_scope_and_order(tmp_path):
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    with sqlite3.connect(storage.path) as db:
        _insert(db, "decision", "task:older:decision",
                {"task_id": "older", "pr_number": 602},
                {"project": "agent_crew", "task_id": "older"}, 1)
        _insert(db, "decision", "task:newer:decision",
                {"task_id": "newer", "pr_number": 602},
                {"project": "agent_crew", "task_id": "newer"}, 2)
        _insert(db, "decision", "task:same-pr:decision",
                {"task_id": "same-pr", "pr_number": 602},
                {"project": "agent_crew", "task_id": "same-pr"}, 4)
        _insert(db, "episodic", "recent-scoped", {"text": "recent"},
                {"project": "agent_crew"}, 5)
        _insert(db, "episodic", "older-scoped", {"text": "older"},
                {"project": "agent_crew"}, 3)
        _insert(db, "decision", "foreign-pr", {"task_id": "foreign", "pr_number": 602},
                {"project": "another"}, 10)
        _insert(db, "episodic", "projectless", {}, {}, 9)
        _insert(db, "episodic", "retired", {"superseded_at": 1},
                {"project": "agent_crew"}, 8)
        db.commit()

    rows, dropped = storage.retrieve_shadow(
        MemoryScope(project="agent_crew", task_id="current"),
        {"decision", "episodic"}, 10, predecessor_task_ids=("older", "newer"),
        pr_number=602,
    )
    assert dropped == 1
    assert rows == [
        MemoryRecord("decision", "task:older:decision",
                     {"task_id": "older", "pr_number": 602},
                     MemoryScope(project="agent_crew", task_id="older")),
        MemoryRecord("decision", "task:newer:decision",
                     {"task_id": "newer", "pr_number": 602},
                     MemoryScope(project="agent_crew", task_id="newer")),
        MemoryRecord("decision", "task:same-pr:decision",
                     {"task_id": "same-pr", "pr_number": 602},
                     MemoryScope(project="agent_crew", task_id="same-pr")),
        MemoryRecord("episodic", "recent-scoped", {"text": "recent"},
                     MemoryScope(project="agent_crew")),
        MemoryRecord("episodic", "older-scoped", {"text": "older"},
                     MemoryScope(project="agent_crew")),
    ]


def test_shadow_query_under_two_cpu_spinners_has_bounded_phase(tmp_path):
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    value_json = json.dumps({"task_id": "old", "pr_number": 602})
    with sqlite3.connect(storage.path) as db:
        db.executemany(
            "INSERT INTO adr001_memory VALUES (?,?,?,?,?,?)",
            (("decision", f"task:old-{i}:decision", value_json,
              json.dumps({"project": "agent_crew" if i < 750 else f"other-{i % 3}"}), 1, i)
             for i in range(3000)),
        )
        db.commit()

    stop = threading.Event()
    durations = []

    def spin():
        while not stop.is_set():
            until = time.perf_counter() + 0.005
            while time.perf_counter() < until:
                pass
            time.sleep(0.005)

    def retrieve_many():
        for _ in range(15):
            timing = {}
            storage.retrieve_shadow(
                MemoryScope(project="agent_crew", task_id="current"),
                {"decision"}, 10, predecessor_task_ids=("old-1", "old-2"),
                pr_number=602, timing=timing,
            )
            durations.append(timing["query_ms"])

    spinners = [threading.Thread(target=spin) for _ in range(2)]
    worker = threading.Thread(target=retrieve_many)
    try:
        for spinner in spinners:
            spinner.start()
        worker.start()
        worker.join(timeout=30)
        assert not worker.is_alive()
    finally:
        stop.set()
        for spinner in spinners:
            spinner.join(timeout=5)
    assert len(durations) == 15
    print(f"shadow query under contention: p50={statistics.median(durations):.1f}ms "
          f"max={max(durations):.1f}ms")
    assert max(durations) < 100
