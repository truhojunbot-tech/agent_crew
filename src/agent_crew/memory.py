"""Provider-neutral, shadow-only optional durable memory contract (#322).

This module deliberately models only optional procedural and episodic recall.
It is not a source of authoritative truth and must not participate in
checkpoint recovery, prompt construction, task routing, or retry decisions.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import logging
from time import perf_counter
import threading
from typing import Optional, Protocol


# Shape only: concrete providers may implement these conceptual stages later.
PIPELINE_STAGES = (
    "authoritative_context",
    "lexical_candidates",
    "semantic_candidates",
    "fusion",
    "optional_rerank",
    "freshness_validation",
    "bounded_results",
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemoryRequest:
    """A scoped request for non-authoritative historical evidence."""

    project: str
    task_id: str = ""
    context_id: str = ""
    coordinator_id: str = ""
    agent_identity: str = ""
    context_generation: Optional[int] = None
    authoritative_ref: str = ""
    branch: str = ""
    commit_ref: str = ""
    memory_types: tuple[str, ...] = ()
    retrieval_mode: str = "shadow"
    limit: int = 10
    pipeline: tuple[str, ...] = PIPELINE_STAGES
    retrieval_query: str = ""
    query_source: str = ""
    issue: str = ""
    predecessor_task_ids: tuple[str, ...] = ()
    pr_number: Optional[int] = None


@dataclass(frozen=True)
class MemoryItem:
    """A provenance-linked, non-authoritative memory candidate."""

    item_id: str
    project: str
    memory_type: str
    source_ref: str
    created_at: str = ""
    version_at: str = ""
    superseded: bool = False
    freshness: str = "unknown"
    rank: Optional[int] = None
    score: Optional[float] = None
    excerpt: str = ""


@dataclass(frozen=True)
class MemoryResult:
    """Outcome of one retrieval attempt; scores are candidates, never truth."""

    provider: str
    backend: str = ""
    state: str = "empty"  # results | empty | unavailable | timeout | error
    items: tuple[MemoryItem, ...] = ()
    latency_ms: float = 0.0
    error_type: str = ""
    error_message: str = ""
    dropped_cross_project: int = 0
    start_delay_ms: Optional[float] = None
    connect_ms: Optional[float] = None
    query_ms: Optional[float] = None


def same_memory_project(request_project: str, item_project: str) -> bool:
    """A memory record crosses this boundary only for its exact project."""
    return bool(request_project) and item_project == request_project


class MemoryProvider(Protocol):
    """Backend-neutral retrieval contract for project and fleet evidence."""

    name: str
    backend: str

    def retrieve(self, request: MemoryRequest) -> MemoryResult:
        """Return optional historical candidates without affecting execution."""
        ...


class NullMemoryProvider:
    """Deterministic default: makes no lookup and reports unavailable."""

    name = "null"
    backend = "none"

    def retrieve(self, request: MemoryRequest) -> MemoryResult:
        return MemoryResult(provider=self.name, backend=self.backend, state="unavailable")


class FakeMemoryProvider:
    """Deterministic in-memory provider for tests; never crosses project scope."""

    name = "fake"
    backend = "in_memory"

    def __init__(self, items: list[MemoryItem] | tuple[MemoryItem, ...] = ()):
        self._items = tuple(items)

    def retrieve(self, request: MemoryRequest) -> MemoryResult:
        # Project is mandatory. An empty/mismatched project is a hard miss.
        if not request.project:
            return MemoryResult(provider=self.name, backend=self.backend, state="empty")
        matching = [item for item in self._items if item.project == request.project]
        if request.memory_types:
            allowed = set(request.memory_types)
            matching = [item for item in matching if item.memory_type in allowed]
        matching = matching[:max(0, request.limit)]
        ranked = tuple(
            item if item.rank is not None else MemoryItem(
                **{**item.__dict__, "rank": index}
            )
            for index, item in enumerate(matching, start=1)
        )
        return MemoryResult(
            provider=self.name,
            backend=self.backend,
            state="results" if ranked else "empty",
            items=ranked,
        )


def shadow_retrieve(provider: MemoryProvider, request: MemoryRequest) -> MemoryResult:
    """Fail-soft wrapper used solely for telemetry at the dispatch seam.

    Only exact project matches cross this boundary, including for fleet-scoped
    storage records returned by a provider.
    """
    started = perf_counter()
    name = getattr(provider, "name", provider.__class__.__name__)
    backend = getattr(provider, "backend", "")
    try:
        result = provider.retrieve(request)
        scoped = tuple(item for item in result.items
                       if same_memory_project(request.project, item.project))
        state = result.state
        if state == "results" and not scoped:
            state = "empty"
        return MemoryResult(
            provider=result.provider or name,
            backend=result.backend or backend,
            state=state,
            items=scoped,
            latency_ms=(perf_counter() - started) * 1000,
            error_type=result.error_type,
            error_message=result.error_message,
            dropped_cross_project=(result.dropped_cross_project
                                   + len(result.items) - len(scoped)),
            connect_ms=result.connect_ms,
            query_ms=result.query_ms,
        )
    except TimeoutError as exc:
        return MemoryResult(
            provider=name, backend=backend, state="timeout",
            latency_ms=(perf_counter() - started) * 1000,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
    except Exception as exc:  # shadow memory is explicitly non-critical
        return MemoryResult(
            provider=name, backend=backend, state="error",
            latency_ms=(perf_counter() - started) * 1000,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )


def shadow_retrieve_bounded(provider: MemoryProvider, request: MemoryRequest,
                            timeout_seconds: float) -> MemoryResult:
    """Observe retrieval without allowing an uncooperative backend to block dispatch.

    The provider runs in a dedicated daemon thread, never in the dispatch event
    loop or a shared executor.  On timeout the thread may remain orphaned, but
    it cannot hold the baseline task past this bounded wait.
    """
    started = perf_counter()
    name = getattr(provider, "name", provider.__class__.__name__)
    backend = getattr(provider, "backend", "")
    done = threading.Event()
    timed_out = threading.Event()
    log_lock = threading.Lock()
    observed = {}

    def log_late_result() -> None:
        with log_lock:
            if observed.get("logged") or not done.is_set():
                return
            observed["logged"] = True
            result = observed["result"]
            total_ms = observed["total_ms"]
        logger.warning(
            "shadow memory retrieval finished after timeout task_id=%s project=%s "
            "start_delay_ms=%.3f connect_ms=%s query_ms=%s total_ms=%.3f "
            "exception_type=%s exception_message=%r",
            request.task_id, request.project, result.start_delay_ms or 0.0,
            f"{result.connect_ms:.3f}" if result.connect_ms is not None else "none",
            f"{result.query_ms:.3f}" if result.query_ms is not None else "none",
            total_ms, result.error_type or "none", result.error_message,
        )

    def observe() -> None:
        start_delay_ms = (perf_counter() - started) * 1000
        try:
            observed["result"] = replace(shadow_retrieve(provider, request),
                                         start_delay_ms=start_delay_ms)
        except BaseException as exc:  # the orphan thread must never raise
            observed["result"] = MemoryResult(
                provider=name, backend=backend, state="error",
                error_type=type(exc).__name__, error_message=str(exc),
                start_delay_ms=start_delay_ms)
        finally:
            observed["total_ms"] = (perf_counter() - started) * 1000
            done.set()
            if timed_out.is_set():
                log_late_result()

    threading.Thread(target=observe, name="agent-crew-shadow-memory", daemon=True).start()
    if done.wait(max(0.0, timeout_seconds)):
        return observed.get("result", MemoryResult(
            provider=name, backend=backend, state="error",
            latency_ms=(perf_counter() - started) * 1000,
            error_type="MissingShadowResult",
        ))
    timed_out.set()
    if done.is_set():
        log_late_result()
    return MemoryResult(
        provider=name, backend=backend, state="timeout",
        latency_ms=(perf_counter() - started) * 1000,
        error_type="ShadowRetrievalTimeout",
    )


def shadow_telemetry(result: MemoryResult, request: MemoryRequest | None = None) -> dict:
    """Content-free telemetry; excerpts never leave the provider boundary."""
    telemetry = {
        "provider": result.provider,
        "backend": result.backend,
        "state": result.state,
        "dropped_cross_project": result.dropped_cross_project,
        "latency_ms": round(result.latency_ms, 3),
        "error_type": result.error_type or None,
        "result_ids": [item.item_id for item in result.items],
        "ranks": [item.rank for item in result.items],
        "scores": [item.score for item in result.items],
        "source_refs": [item.source_ref for item in result.items],
        "freshness": [item.freshness for item in result.items],
        "superseded": [item.superseded for item in result.items],
    }
    if result.state != "timeout" and result.start_delay_ms is not None:
        telemetry["start_delay_ms"] = round(result.start_delay_ms, 3)
    if request is not None and request.retrieval_query:
        telemetry["query_hash"] = hashlib.sha256(
            request.retrieval_query.encode("utf-8")
        ).hexdigest()[:16]
        telemetry["query_source"] = request.query_source
    return telemetry
