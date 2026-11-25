"""#1032: a fallback caused only by the 300 ms retrieval budget gets one retry.

ADR-001 §9 fixes 300 ms as the retrieval budget, so the first attempt keeps
it. Only a budget-exceeded first attempt is retried, once, with a larger
budget; a non-timeout error or a still-slow retry falls back as before and
the fallback stays counted.
"""
import sqlite3
import threading
import time

import pytest

from agent_crew import memory_hybrid
from agent_crew.memory_hybrid import HybridMemoryStorage, LATENCY_BUDGET_SECONDS
from agent_crew.memory_runtime import MemoryRecord, MemoryScope

SLOW_ONCE = LATENCY_BUDGET_SECONDS + 0.05
ALWAYS_SLOW = LATENCY_BUDGET_SECONDS * memory_hybrid.LATENCY_RETRY_BUDGET_MULTIPLIER + 0.1


@pytest.fixture
def store(tmp_path):
    storage = HybridMemoryStorage(str(tmp_path / "memory.db"), embedder=None)
    storage.put(MemoryRecord("episodic", "signal:one", {"text": "signal"},
                             MemoryScope(project="alfred")))
    return storage


def query(store, **kwargs):
    return store.retrieve_ranked(MemoryScope(project="alfred"), "signal",
                                 "implementer", 20, 20000, **kwargs)


def slow_candidates(store, delays):
    """Replace the candidate lookup with one that sleeps per call, then delegates."""
    original = store._candidates
    calls = []

    def stub(db, scope, query=""):
        calls.append(time.perf_counter())
        time.sleep(delays[min(len(calls), len(delays)) - 1])
        return original(db, scope, query)

    store._candidates = stub
    return calls


def test_budget_exceeded_once_then_fast_is_not_a_fallback(store):
    calls = slow_candidates(store, [SLOW_ONCE, 0.0])
    result = query(store)
    assert result["mode"] == "lexical_only"
    assert [row["key"] for row in result["middle"]] == ["signal:one"]
    assert len(calls) == 2
    assert store.fallback_count == 0
    assert store.budget_retry_count == 1
    assert store.last_retrieval_mode == "lexical_only"


def test_always_slow_still_falls_back_after_one_retry(store):
    calls = slow_candidates(store, [ALWAYS_SLOW])
    result = query(store)
    assert result["mode"] == "fallback"
    assert len(calls) == 2
    assert store.fallback_count == 1
    assert store.budget_retry_count == 1
    assert store.last_retrieval_mode == "fallback"


def test_non_timeout_error_is_never_retried(store):
    calls = []

    def broken(db, scope, query=""):
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    store._candidates = broken
    result = query(store)
    assert result["mode"] == "fallback"
    assert len(calls) == 1
    assert store.fallback_count == 1
    assert store.budget_retry_count == 0


def test_budget_exceeded_at_render_check_is_retried(store):
    """The post-render budget check (previously L510) is a timeout-only cause too."""
    original = store.effective_rows
    calls = []

    def slow_effective(rows, scope):
        calls.append(1)
        if len(calls) <= 2:  # head + middle rejoin of the first attempt
            time.sleep(SLOW_ONCE / 2)
        return original(rows, scope)

    store.effective_rows = slow_effective
    result = query(store)
    assert result["mode"] == "lexical_only"
    assert len(calls) == 4
    assert store.fallback_count == 0
    assert store.budget_retry_count == 1


def test_no_retry_once_the_caller_already_served_a_fallback(store):
    """A dispatcher that timed out at 300 ms has already answered; do not spend a retry."""
    calls = slow_candidates(store, [SLOW_ONCE])
    marker = threading.Event()
    marker.set()
    result = query(store, fallback_marker=marker)
    assert result["mode"] == "fallback"
    assert len(calls) == 1
    assert store.fallback_count == 0  # the caller's fallback is the one counted
    assert store.budget_retry_count == 0


def test_hand_built_store_without_retry_counter_still_retries(tmp_path):
    """alfred/tools/decision_rule_gate.py builds the store with __new__ and sets
    only path/embedder/model_id/fallback_count/_fallback_lock."""
    seeded = HybridMemoryStorage(str(tmp_path / "memory.db"), embedder=None)
    seeded.put(MemoryRecord("procedural", "rule:deploy", {"text": "deploy rule"},
                            MemoryScope(project="alfred", fleet="fleet")))
    store = HybridMemoryStorage.__new__(HybridMemoryStorage)
    store.path = seeded.path
    store.embedder = None
    store.model_id = ""
    store.fallback_count = 0
    store._fallback_lock = threading.Lock()
    slow_candidates(store, [SLOW_ONCE, 0.0])
    result = store.retrieve_ranked(MemoryScope(project="alfred", fleet="fleet"),
                                   "rule deploy", "coordinator", 96, 65536)
    assert result["mode"] == "lexical_only"
    assert "rule:deploy" in [row["key"] for row in result["middle"]]
    assert store.fallback_count == 0
    assert store.budget_retry_count == 1
