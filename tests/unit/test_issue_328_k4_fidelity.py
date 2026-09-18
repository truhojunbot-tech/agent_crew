"""#328 — K4 queries are distinct from K1/K2 identity fields."""
from __future__ import annotations

import hashlib

from agent_crew.k4_shadow_experiment import (
    ExactLookupProvider,
    HybridProvider,
    LexicalProvider,
    build_experiment,
    run_experiment,
)
from agent_crew.memory import MemoryRequest, MemoryResult, shadow_telemetry


def test_memory_request_keeps_identity_and_query_defaults_empty():
    request = MemoryRequest(project="agent_crew")

    assert request.context_id == ""
    assert request.retrieval_query == ""
    assert request.query_source == ""


def test_k4_scorers_do_not_read_context_identity_as_a_query():
    corpus, _ = build_experiment()
    request = MemoryRequest(project="agent_crew", context_id="#301")
    record = next(record for record in corpus if record.item.item_id == "agent-crew-301-misdiagnosis")

    assert ExactLookupProvider(corpus).score(record, request) == 0.0
    assert LexicalProvider(corpus).score(record, request) == 0.0
    assert HybridProvider(corpus).score(record, request) == 0.0


def test_shadow_telemetry_without_request_retains_its_existing_shape():
    telemetry = shadow_telemetry(MemoryResult(provider="null", backend="none", state="unavailable"))

    assert set(telemetry) == {
        "provider", "backend", "state", "latency_ms", "error_type", "result_ids", "ranks",
        "scores", "source_refs", "freshness", "superseded",
    }
    assert "query_hash" not in telemetry
    assert "query_source" not in telemetry


def test_shadow_telemetry_fingerprints_a_nonempty_query_without_exposing_it():
    query = "Has this exact codex exec sandbox hang signature been seen before on this test/suite, and was it ever a real bug or always an artifact?"
    request = MemoryRequest(
        project="agent_crew", retrieval_query=query, query_source="testimony_derived"
    )

    telemetry = shadow_telemetry(MemoryResult(provider="null"), request)

    assert telemetry["query_hash"] == hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
    assert telemetry["query_source"] == "testimony_derived"
    assert query not in repr(telemetry)


def test_experiment_queries_are_provenance_tagged_and_identity_free():
    _, queries = build_experiment()

    assert len(queries) == 12
    for query in queries:
        assert query.source
        assert query.original_query
        assert query.effective_query
        assert query.label_provenance in {
            "TESTIMONY_DERIVED", "TASK_DESCRIPTION", "ROOT_CAUSE_QUESTION",
        }
        assert query.request.retrieval_query == query.effective_query
        assert query.request.context_id == ""


def test_experiment_writes_artifact_without_declaring_a_winner(tmp_path):
    artifact_path = tmp_path / "k4-shadow-evidence.json"

    artifact = run_experiment(artifact_path)

    assert artifact["winner"] is None
    assert artifact_path.exists()
