"""Provider-neutral, shadow-only optional durable memory contract (#322).

This module deliberately models only optional procedural and episodic recall.
It is not a source of authoritative truth and must not participate in
checkpoint recovery, prompt construction, task routing, or retry decisions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
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


@dataclass(frozen=True)
class MemoryRequest:
    """A project-local request for non-authoritative historical evidence."""

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


class MemoryProvider(Protocol):
    """Backend-neutral retrieval contract. Implementations must be project-local."""

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

    It also filters a provider's response by the mandatory request project.
    This is defense in depth: a future backend cannot cause a cross-project
    result merely by failing to enforce the contract itself.
    """
    started = perf_counter()
    name = getattr(provider, "name", provider.__class__.__name__)
    backend = getattr(provider, "backend", "")
    try:
        result = provider.retrieve(request)
        scoped = tuple(item for item in result.items if item.project == request.project)
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
        )
    except TimeoutError as exc:
        return MemoryResult(
            provider=name, backend=backend, state="timeout",
            latency_ms=(perf_counter() - started) * 1000,
            error_type=type(exc).__name__,
        )
    except Exception as exc:  # shadow memory is explicitly non-critical
        return MemoryResult(
            provider=name, backend=backend, state="error",
            latency_ms=(perf_counter() - started) * 1000,
            error_type=type(exc).__name__,
        )


def shadow_telemetry(result: MemoryResult) -> dict:
    """Content-free telemetry; excerpts never leave the provider boundary."""
    return {
        "provider": result.provider,
        "backend": result.backend,
        "state": result.state,
        "latency_ms": round(result.latency_ms, 3),
        "error_type": result.error_type or None,
        "result_ids": [item.item_id for item in result.items],
        "ranks": [item.rank for item in result.items],
        "scores": [item.score for item in result.items],
        "source_refs": [item.source_ref for item in result.items],
        "freshness": [item.freshness for item in result.items],
        "superseded": [item.superseded for item in result.items],
    }
