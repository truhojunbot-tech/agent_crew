"""Offline-only K4 retrieval comparison fixture and evidence runner (#326).

Nothing in this module is imported by dispatch code.  It exists solely for the
pytest experiment that compares deterministic candidate retrieval behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from pathlib import Path
from time import perf_counter, sleep
from typing import Iterable

from .memory import (
    MemoryItem, MemoryProvider, MemoryRequest, MemoryResult, shadow_retrieve,
    shadow_retrieve_bounded,
)


# Portable identifiers retain the testimony filename without embedding an
# external vault path in the agent_crew core package.
TESTIMONY_SOURCE_PREFIX = "council-126-testimony/"


@dataclass(frozen=True)
class CorpusRecord:
    """A fixed, traceable testimony-derived retrieval record."""

    item: MemoryItem
    text: str
    exact_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExperimentQuery:
    """A representative historical question and its deterministic judgment."""

    query_id: str
    source: str
    original_query: str
    effective_query: str
    label_provenance: str
    request: MemoryRequest
    expected_ids: frozenset[str]
    prohibited_ids: frozenset[str] = frozenset()


def _record(
    item_id: str, project: str, document: str, text: str, *,
    superseded: bool = False, exact_keys: tuple[str, ...] = (),
) -> CorpusRecord:
    return CorpusRecord(
        item=MemoryItem(
            item_id=item_id,
            project=project,
            memory_type="episodic",
            source_ref=TESTIMONY_SOURCE_PREFIX + document,
            created_at="2026-09-18T00:00:00Z",
            version_at="2026-09-18T00:00:00Z",
            superseded=superseded,
            freshness="historical",
            excerpt=text,
        ),
        text=text,
        exact_keys=exact_keys,
    )


def build_experiment() -> tuple[tuple[CorpusRecord, ...], tuple[ExperimentQuery, ...]]:
    """Return the fixed 17-record corpus and representative query judgments.

    Records faithfully paraphrase the supplied Round 1 testimony summaries.
    The one constructed record is explicitly marked and exists only to prove
    structural cross-project scope isolation with an otherwise high-scoring key.
    """
    corpus = (
        _record("agent-crew-301-misdiagnosis", "agent_crew", "agent_crew.md",
                "Issue #301 closed as a misdiagnosis: the theory that the reviewed branch lived only in another repository was falsified by git ls-remote and log evidence. Concurrent Codex usage-limit exhaustion was the actual cause.",
                exact_keys=("#301", "agent_crew#301")),
        _record("agent-crew-300-original-close", "agent_crew", "agent_crew.md",
                "Issue #300 was closed on 2026-09-13 after role alternation was ruled out.",
                superseded=True, exact_keys=("#300", "agent_crew#300")),
        _record("agent-crew-300-reopened", "agent_crew", "agent_crew.md",
                "Issue #300 later reopened: broader evidence showed same-provider retries beyond the original role-alternation close. Its current status is open.",
                exact_keys=("#300", "agent_crew#300")),
        _record("agent-crew-434-rerejection", "kis-trader", "kis-trader.md",
                "Issue #434 records that after context compaction the coordinator did not see task #36 had already been rejected, and rejected the settled approach again.",
                exact_keys=("#434", "kis-trader#434")),
        _record("kis-task-36-rejected", "kis-trader", "kis-trader.md",
                "Task #36 rejected the closing-auction imbalance entry filter because its statistical test at n=28 then n=40 showed no edge.",
                exact_keys=("#36", "task#36")),
        _record("kis-task-40-rejected", "kis-trader", "kis-trader.md",
                "Task #40 rejected direct AI stock selection because the evidence had insufficient statistical power.",
                exact_keys=("#40", "task#40")),
        _record("metaculus-signal-positive-snapshot", "metaculus", "metaculus.md",
                "An earlier live-betting-readiness backtest snapshot appeared positive before reversal; the later state file labels this snapshot _SUPERSEDED.",
                superseded=True),
        _record("metaculus-signal-reverted-current", "metaculus", "metaculus.md",
                "The Metaculus live-betting-readiness signal reverted twice: n=45 to 59 on 2026-08-01 and n=67 to 71 on 2026-08-05. Earlier positive state is superseded.",
                exact_keys=("state.json",)),
        _record("metaculus-reversal-procedure", "metaculus", "metaculus.md",
                "Backtest readiness requires checking the newest state rather than treating an earlier positive result as still valid."),
        _record("quota-crew-run-banned", "quota", "quota.md",
                "Crew run is banned for the affected canary because REVIEW_RETRY_MAX=2 caused runaway retries; this is a rejected tool-usage pattern.",
                exact_keys=("crew run", "REVIEW_RETRY_MAX")),
        _record("quota-review-retry-incident", "quota", "quota.md",
                "The retry budget incident requires a bounded retry count and records REVIEW_RETRY_MAX as a canary safety constraint.",
                exact_keys=("REVIEW_RETRY_MAX",)),
        _record("agent-crew-reviewed-sha", "agent_crew", "agent_crew.md",
                "A review must pin and verify the reviewed commit so a report cannot describe a different branch as if it were the task branch."),
        _record("agent-crew-review-not-run", "agent_crew", "agent_crew.md",
                "A reviewer process that did not run has no findings; retry exhaustion must not be reported as a review rejection."),
        _record("kis-compaction-consult-history", "kis-trader", "kis-trader.md",
                "Following compaction, consult existing rejected experiments before repeating a statistical test."),
        _record("metaculus-sample-size-caveat", "metaculus", "metaculus.md",
                "A changing sample size can reverse an apparently positive signal, so dated snapshots need currentness checks."),
        _record("quota-retry-observability", "quota", "quota.md",
                "Retry counters need explicit observability so a bounded policy does not conceal repeated failed attempts."),
        # Constructed fixture only: it deliberately shares a high-overlap key
        # with quota records and is not represented as fleet testimony.
        _record("constructed-agent-crew-review-retry", "agent_crew", "agent_crew.md",
                "Constructed scope-isolation fixture: REVIEW_RETRY_MAX is an unrelated documentation label for this project.",
                exact_keys=("REVIEW_RETRY_MAX",)),
    )
    def query(
        query_id: str, source: str, original_query: str, effective_query: str,
        label_provenance: str, project: str, expected: Iterable[str],
        prohibited: Iterable[str] = (),
    ) -> ExperimentQuery:
        return ExperimentQuery(
            query_id=query_id,
            source=source,
            original_query=original_query,
            effective_query=effective_query,
            label_provenance=label_provenance,
            request=MemoryRequest(
                project=project, task_id="328", retrieval_query=effective_query,
                query_source=label_provenance.lower(), limit=10,
            ),
            expected_ids=frozenset(expected), prohibited_ids=frozenset(prohibited),
        )
    queries = (
        query("td-codex-sandbox-hang", "council-126-testimony/agent_crew.md#8", "Has this exact codex exec sandbox hang signature been seen before on this test/suite, and was it ever a real bug or always an artifact?", "Has this exact codex exec sandbox hang signature been seen before on this test/suite, and was it ever a real bug or always an artifact?", "TESTIMONY_DERIVED", "agent_crew", []),
        query("td-telemetry-offset-mismatch", "council-126-testimony/agent_crew.md#8", "What was the actual root cause the last time a telemetry/context-boundary test failed with a similar offset/session_id mismatch symptom?", "What was the actual root cause the last time a telemetry/context-boundary test failed with a similar offset/session_id mismatch symptom?", "TESTIMONY_DERIVED", "agent_crew", []),
        query("td-stop-gating-rejected-approaches", "council-126-testimony/agent_crew.md#8", "What prior approaches to fixing this class of STOP-propagation / pause-gating gap were tried and rejected, and why?", "What prior approaches to fixing this class of STOP-propagation / pause-gating gap were tried and rejected, and why?", "TESTIMONY_DERIVED", "agent_crew", ["agent-crew-301-misdiagnosis", "agent-crew-300-reopened"], ["agent-crew-300-original-close"]),
        query("td-issue-300-current-status", "council-126-testimony/agent_crew.md#8", "What is the current, up-to-date status of issue #300 — is it still open, and what's the latest evidence, superseding any earlier close?", "What is the current, up-to-date status of issue #300 — is it still open, and what's the latest evidence, superseding any earlier close?", "TESTIMONY_DERIVED", "agent_crew", ["agent-crew-300-reopened"], ["agent-crew-300-original-close"]),
        query("td-fstring-python-version-gotcha", "council-126-testimony/agent_crew.md#8", "Has this exact Python-version (3.10 vs 3.12) f-string gotcha class been hit before in this repo, and how was it fixed?", "Has this exact Python-version (3.10 vs 3.12) f-string gotcha class been hit before in this repo, and how was it fixed?", "TESTIMONY_DERIVED", "agent_crew", []),
        query("td-issue-300-title", "github_issue:agent_crew#300 (title)", "possible regression: shared-branch-ref corruption symptoms recurring on alpha_engine after implementer role now alternates claude/codex", "possible regression: shared-branch-ref corruption symptoms recurring on alpha_engine after implementer role now alternates claude/codex", "TASK_DESCRIPTION", "agent_crew", ["agent-crew-300-reopened"], ["agent-crew-300-original-close"]),
        query("td-issue-301-title", "github_issue:agent_crew#301 (title)", "review tasks for a branch in another repo fail exit_1 and drive an implement/review loop", "review tasks for a branch in another repo fail exit_1 and drive an implement/review loop", "TASK_DESCRIPTION", "agent_crew", ["agent-crew-301-misdiagnosis"]),
        query("td-issue-313-title", "github_issue:agent_crew#313 (title)", "[P0][SAFETY] Gate result cascades + make claim pause check atomic under runtime STOP", "[P0][SAFETY] Gate result cascades + make claim pause check atomic under runtime STOP", "TASK_DESCRIPTION", "agent_crew", []),
        query("rc-no-project-wide-resume", "state.json#ruled_out (2026-09-18 snapshot)", "Using a project-wide gen6/full-project resume of agent_crew's queue/dispatcher to run an independent code review or a shadow/offline experiment", "Using a project-wide gen6/full-project resume of agent_crew's queue/dispatcher to run an independent code review or a shadow/offline experiment", "ROOT_CAUSE_QUESTION", "agent_crew", []),
        query("rc-no-raw-sql-park-backlog", "state.json#ruled_out (2026-09-18 snapshot)", "Temporarily parking unrelated pending backlog tasks (e.g. via a raw SQL status='blocked' update) as a workaround to allow a scoped project resume without risking those tasks being claimed", "Temporarily parking unrelated pending backlog tasks (e.g. via a raw SQL status='blocked' update) as a workaround to allow a scoped project resume without risking those tasks being claimed", "ROOT_CAUSE_QUESTION", "agent_crew", []),
        query("rc-no-trust-codex-self-report", "state.json#ruled_out (2026-09-18 snapshot)", "Trusting codex exec's own self-reported test pass/fail counts, root-cause diagnoses, or portability-grep results without independent re-verification", "Trusting codex exec's own self-reported test pass/fail counts, root-cause diagnoses, or portability-grep results without independent re-verification", "ROOT_CAUSE_QUESTION", "agent_crew", []),
        query("rc-review-retry-max-scope", "github_issue:agent_crew#326 review round1 finding", "What does REVIEW_RETRY_MAX mean for this task?", "What does REVIEW_RETRY_MAX mean for this task?", "ROOT_CAUSE_QUESTION", "quota", ["quota-review-retry-incident", "quota-crew-run-banned"], ["constructed-agent-crew-review-retry"]),
    )
    return corpus, queries


def _tokens(text: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9_#]+", text.lower())
    return frozenset(_stem(word) for word in words if len(word) > 1 or word.startswith("#"))


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[:-len(suffix)]
    return word


class _CorpusProvider:
    """Shared local corpus mechanics; subclasses differ only in scoring."""

    name = "corpus"
    backend = "offline_fixture"

    def __init__(self, corpus: Iterable[CorpusRecord]):
        self._corpus = tuple(corpus)

    def score(self, record: CorpusRecord, request: MemoryRequest) -> float:
        raise NotImplementedError

    def retrieve(self, request: MemoryRequest) -> MemoryResult:
        started = perf_counter()
        ranked: list[tuple[float, CorpusRecord]] = []
        for record in self._corpus:
            score = self.score(record, request)
            if score > 0:
                ranked.append((score, record))
        ranked.sort(key=lambda pair: (-pair[0], pair[1].item.item_id))
        items = tuple(
            MemoryItem(**{**record.item.__dict__, "rank": rank, "score": round(score, 6)})
            for rank, (score, record) in enumerate(ranked[:max(0, request.limit)], start=1)
        )
        return MemoryResult(provider=self.name, backend=self.backend,
                            state="results" if items else "empty", items=items,
                            latency_ms=(perf_counter() - started) * 1000)


class ExactLookupProvider(_CorpusProvider):
    """No-new-backend baseline: identifiers and exact keys only."""

    name = "exact_lookup"

    def score(self, record: CorpusRecord, request: MemoryRequest) -> float:
        question = request.retrieval_query.lower()
        return float(sum(key.lower() in question for key in record.exact_keys))


class LexicalProvider(_CorpusProvider):
    """Token-overlap-only local scorer; no semantic signal."""

    name = "lexical"

    def score(self, record: CorpusRecord, request: MemoryRequest) -> float:
        return float(len(_tokens(request.retrieval_query) & _tokens(record.text)))


class HybridProvider(_CorpusProvider):
    """Lexical score fused with deterministic stemmed-token Jaccard similarity."""

    name = "hybrid"

    def score(self, record: CorpusRecord, request: MemoryRequest) -> float:
        query_tokens = _tokens(request.retrieval_query)
        record_tokens = _tokens(record.text)
        overlap = len(query_tokens & record_tokens)
        if not overlap:
            return 0.0
        return float(overlap) + overlap / len(query_tokens | record_tokens)


class ScopeIsolatedProvider:
    """Structural project filter around any provider, independent of its scorer."""

    backend = "scope_wrapper"

    def __init__(self, provider: MemoryProvider):
        self._provider = provider
        self.name = f"scope_isolated({provider.name})"

    def retrieve(self, request: MemoryRequest) -> MemoryResult:
        result = self._provider.retrieve(request)
        items = tuple(item for item in result.items if item.project == request.project)
        return MemoryResult(provider=self.name, backend=self.backend,
                            state="results" if items else "empty", items=items,
                            latency_ms=result.latency_ms, error_type=result.error_type)


class SupersededHardRejectProvider:
    """Policy wrapper that removes superseded history rather than downranking it."""

    backend = "hard_reject_wrapper"

    def __init__(self, provider: MemoryProvider):
        self._provider = provider
        self.name = f"{provider.name}_hard_reject"

    def retrieve(self, request: MemoryRequest) -> MemoryResult:
        result = self._provider.retrieve(request)
        items = tuple(item for item in result.items if not item.superseded)
        return MemoryResult(provider=self.name, backend=self.backend,
                            state="results" if items else "empty", items=items,
                            latency_ms=result.latency_ms, error_type=result.error_type)


class HangingProvider:
    """Deliberately uncooperative provider used only to exercise bounded retrieval."""

    name = "hanging_fixture"
    backend = "offline_fault_injection"

    def retrieve(self, request: MemoryRequest) -> MemoryResult:
        sleep(2)
        return MemoryResult(provider=self.name, backend=self.backend, state="empty")


def build_candidates(corpus: tuple[CorpusRecord, ...]) -> dict[str, MemoryProvider]:
    return {
        "exact_lookup": ScopeIsolatedProvider(ExactLookupProvider(corpus)),
        "lexical": ScopeIsolatedProvider(LexicalProvider(corpus)),
        "hybrid": ScopeIsolatedProvider(HybridProvider(corpus)),
        "lexical_hard_reject": ScopeIsolatedProvider(SupersededHardRejectProvider(LexicalProvider(corpus))),
        "hybrid_hard_reject": ScopeIsolatedProvider(SupersededHardRejectProvider(HybridProvider(corpus))),
    }


def run_experiment(artifact_path: Path | str | None = None) -> dict:
    """Execute the comparison and optionally write a structured JSON artifact."""
    corpus, queries = build_experiment()
    results = []
    for candidate, provider in build_candidates(corpus).items():
        for query in queries:
            result = shadow_retrieve(provider, query.request)
            returned_ids = [item.item_id for item in result.items]
            returned = set(returned_ids)
            relevant = query.expected_ids
            matched = sorted(returned & relevant)
            prohibited = sorted(returned & query.prohibited_ids)
            precision = len(matched) / len(returned) if returned else 0.0
            recall = len(matched) / len(relevant) if relevant else 1.0
            results.append({
                "candidate": candidate,
                "query_id": query.query_id,
                "state": result.state,
                "latency_ms": round(result.latency_ms, 3),
                "returned_ids": returned_ids,
                "source_refs": [item.source_ref for item in result.items],
                "expected_ids": sorted(relevant),
                "prohibited_ids": sorted(query.prohibited_ids),
                "matched_ids": matched,
                "prohibited_ids_returned": prohibited,
                "precision": round(precision, 3),
                "recall": round(recall, 3),
                "hit": bool(matched) and not prohibited,
            })
    fault = shadow_retrieve_bounded(
        HangingProvider(), MemoryRequest(project="agent_crew", task_id="326"), timeout_seconds=0.05)
    artifact = {
        "purpose": "shadow_evidence_only",
        "winner": None,
        "corpus_records": len(corpus),
        "queries": [{"query_id": query.query_id, "expected_ids": sorted(query.expected_ids),
                     "prohibited_ids": sorted(query.prohibited_ids)} for query in queries],
        "results": results,
        "fault_injection": {"state": fault.state, "error_type": fault.error_type,
                            "latency_ms": round(fault.latency_ms, 3)},
    }
    if artifact_path is not None:
        Path(artifact_path).write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    return artifact
