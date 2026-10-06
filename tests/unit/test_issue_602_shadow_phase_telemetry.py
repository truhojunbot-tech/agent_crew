"""Phase telemetry for bounded shadow-memory retrieval (#602)."""

import logging
import sqlite3
import time

from agent_crew.memory import (MemoryRequest, MemoryResult, shadow_retrieve,
                               shadow_retrieve_bounded, shadow_telemetry)
from agent_crew.memory_runtime import (MemoryRecord, MemoryScope,
                                       RuntimeMemoryProvider, SQLiteMemoryStorage)


def _late_warning(caplog, task_id, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = [record.getMessage() for record in caplog.records
                   if record.name == "agent_crew.memory"
                   and task_id in record.getMessage()
                   and "finished after timeout" in record.getMessage()]
        if matches:
            return matches
        time.sleep(0.01)
    return []


def test_late_provider_returns_within_bound_then_logs_phases(caplog):
    class SlowProvider:
        name = "slow"
        backend = "test"

        def retrieve(self, request):
            time.sleep(0.12)
            return MemoryResult(provider=self.name, backend=self.backend,
                                state="empty", connect_ms=3.0, query_ms=4.0)

    request = MemoryRequest(project="agent_crew", task_id="slow-602")
    with caplog.at_level(logging.WARNING, logger="agent_crew.memory"):
        started = time.monotonic()
        result = shadow_retrieve_bounded(SlowProvider(), request, 0.02)
        elapsed = time.monotonic() - started
        warnings = _late_warning(caplog, request.task_id)
    assert result.state == "timeout" and elapsed < 0.1
    assert len(warnings) == 1
    for field in ("task_id=slow-602", "project=agent_crew", "start_delay_ms=",
                  "connect_ms=3", "query_ms=4", "total_ms="):
        assert field in warnings[0]


def test_locked_sqlite_logs_operational_error_after_timeout(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("AGENT_CREW_SHADOW_MEMORY_TIMEOUT_SECONDS", "0.08")
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    scope = MemoryScope(project="agent_crew")
    storage.put_many_shadow([MemoryRecord("episodic", "lock-602", {}, scope)])
    holder = sqlite3.connect(storage.path)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        request = MemoryRequest(project="agent_crew", task_id="lock-602",
                                memory_types=("episodic",))
        with caplog.at_level(logging.WARNING, logger="agent_crew.memory"):
            started = time.monotonic()
            result = shadow_retrieve_bounded(RuntimeMemoryProvider(storage), request, 0.01)
            elapsed = time.monotonic() - started
            warnings = _late_warning(caplog, request.task_id)
        assert result.state == "timeout" and elapsed < 0.1
        assert len(warnings) == 1
        assert "OperationalError" in warnings[0]
        assert "database is locked" in warnings[0]
        assert "connect_ms=none" not in warnings[0]
        assert "query_ms=none" not in warnings[0]
    finally:
        holder.rollback()
        holder.close()


def test_successful_retrieval_reports_start_delay():
    class FastProvider:
        name = "fast"
        backend = "test"

        def retrieve(self, request):
            return MemoryResult(provider=self.name, backend=self.backend, state="empty")

    result = shadow_retrieve_bounded(
        FastProvider(), MemoryRequest(project="agent_crew", task_id="fast-602"), 0.25)
    assert result.state == "empty"
    assert shadow_telemetry(result)["start_delay_ms"] >= 0


def test_sqlite_result_exposes_connect_and_query_phases(tmp_path):
    storage = SQLiteMemoryStorage(str(tmp_path / "memory.db"))
    scope = MemoryScope(project="agent_crew")
    storage.put_many_shadow([MemoryRecord("episodic", "phase-602", {}, scope)])
    result = shadow_retrieve(RuntimeMemoryProvider(storage), MemoryRequest(
        project="agent_crew", task_id="phase-602", memory_types=("episodic",)))
    assert result.state == "results"
    assert result.connect_ms is not None and result.connect_ms >= 0
    assert result.query_ms is not None and result.query_ms >= 0
