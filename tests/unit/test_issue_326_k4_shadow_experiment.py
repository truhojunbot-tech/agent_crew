"""#326 — offline K4 retrieval comparison over Round 1 fleet evidence only."""
from __future__ import annotations

from time import perf_counter

from agent_crew.k4_shadow_experiment import (
    ExactLookupProvider,
    HangingProvider,
    HybridProvider,
    LexicalProvider,
    ScopeIsolatedProvider,
    SupersededHardRejectProvider,
    TESTIMONY_SOURCE_PREFIX,
    build_candidates,
    build_experiment,
    run_experiment,
)
from agent_crew.memory import MemoryRequest, shadow_retrieve, shadow_retrieve_bounded


def test_full_experiment_emits_ground_truth_comparison_artifact(tmp_path):
    artifact_path = tmp_path / "k4-shadow-evidence.json"

    artifact = run_experiment(artifact_path)

    assert artifact_path.exists()
    assert artifact["purpose"] == "shadow_evidence_only"
    assert artifact["winner"] is None
    assert len(artifact["queries"]) >= 7
    assert {entry["candidate"] for entry in artifact["results"]} == {
        "exact_lookup", "lexical", "hybrid", "lexical_hard_reject", "hybrid_hard_reject",
    }
    assert all("latency_ms" in entry for entry in artifact["results"])
    assert all("precision" in entry and "recall" in entry for entry in artifact["results"])
    assert all("hit" in entry for entry in artifact["results"])
    assert all(
        source_ref.startswith(TESTIMONY_SOURCE_PREFIX)
        for entry in artifact["results"] for source_ref in entry["source_refs"]
    )

    by_candidate_query = {
        (entry["candidate"], entry["query_id"]): entry for entry in artifact["results"]
    }
    for candidate in ("lexical_hard_reject", "hybrid_hard_reject"):
        candidate_rows = [entry for entry in artifact["results"] if entry["candidate"] == candidate]
        assert len(candidate_rows) == len(artifact["queries"])
        assert all(entry["recall"] == 1.0 and entry["hit"] for entry in candidate_rows)
    for candidate in ("lexical", "hybrid"):
        candidate_rows = [entry for entry in artifact["results"] if entry["candidate"] == candidate]
        assert all(entry["recall"] == 1.0 for entry in candidate_rows)
    assert by_candidate_query[("lexical_hard_reject", "issue-300-current-status")]["prohibited_ids_returned"] == []
    assert by_candidate_query[("hybrid_hard_reject", "metaculus-reversal-currentness")]["prohibited_ids_returned"] == []
    assert by_candidate_query[("lexical", "issue-300-current-status")]["prohibited_ids_returned"] == [
        "agent-crew-300-original-close"
    ]


def test_superseded_hard_reject_excludes_issue_300_and_metaculus_history():
    corpus, _ = build_experiment()
    query_by_id = {query.query_id: query for query in build_experiment()[1]}
    request_300 = query_by_id["issue-300-current-status"].request
    request_metaculus = query_by_id["metaculus-reversal-currentness"].request

    lexical_300 = shadow_retrieve(ScopeIsolatedProvider(LexicalProvider(corpus)), request_300)
    rejected_300 = shadow_retrieve(
        ScopeIsolatedProvider(SupersededHardRejectProvider(LexicalProvider(corpus))), request_300)
    lexical_metaculus = shadow_retrieve(ScopeIsolatedProvider(LexicalProvider(corpus)), request_metaculus)
    rejected_metaculus = shadow_retrieve(
        ScopeIsolatedProvider(SupersededHardRejectProvider(HybridProvider(corpus))), request_metaculus)

    assert "agent-crew-300-original-close" in {item.item_id for item in lexical_300.items}
    assert "agent-crew-300-original-close" not in {item.item_id for item in rejected_300.items}
    assert "agent-crew-300-reopened" in {item.item_id for item in rejected_300.items}
    assert "metaculus-signal-positive-snapshot" in {item.item_id for item in lexical_metaculus.items}
    assert "metaculus-signal-positive-snapshot" not in {item.item_id for item in rejected_metaculus.items}
    assert "metaculus-signal-reverted-current" in {item.item_id for item in rejected_metaculus.items}


def test_scope_wrapper_and_shadow_defense_in_depth_prevent_cross_project_result():
    corpus, queries = build_experiment()
    scoped = ScopeIsolatedProvider(LexicalProvider(corpus))
    quota_request = next(query.request for query in queries if query.query_id == "quota-retry-scope")

    result = shadow_retrieve(scoped, quota_request)
    agent_crew_request = next(
        query.request for query in queries if query.query_id == "agent-crew-retry-scope")
    agent_crew_result = shadow_retrieve(scoped, agent_crew_request)

    assert result.state == "results"
    assert {item.project for item in result.items} == {"quota"}
    assert "constructed-agent-crew-review-retry" not in {item.item_id for item in result.items}
    assert {item.project for item in agent_crew_result.items} == {"agent_crew"}
    assert "quota-review-retry-incident" not in {item.item_id for item in agent_crew_result.items}


def test_every_result_has_traceable_testimony_provenance():
    corpus, queries = build_experiment()
    for provider in build_candidates(corpus).values():
        for query in queries:
            result = shadow_retrieve(provider, query.request)
            for item in result.items:
                assert item.source_ref.startswith(TESTIMONY_SOURCE_PREFIX)
                assert item.source_ref.endswith(".md")
                assert any(record.item.item_id == item.item_id for record in corpus)


def test_fixture_uses_only_real_testimony_documents_and_projects():
    corpus, queries = build_experiment()

    assert len(corpus) == 17
    assert {record.item.project for record in corpus} <= {
        "agent_crew", "kis-trader", "metaculus", "quota",
    }
    assert {
        record.item.source_ref.removeprefix(TESTIMONY_SOURCE_PREFIX) for record in corpus
    } <= {"agent_crew.md", "kis-trader.md", "metaculus.md", "quota.md"}
    assert "sibling-function-shape" not in {query.query_id for query in queries}


def test_hanging_provider_is_bounded_by_existing_shadow_primitive():
    started = perf_counter()
    result = shadow_retrieve_bounded(
        HangingProvider(),
        MemoryRequest(project="agent_crew", task_id="326"),
        timeout_seconds=0.05,
    )
    elapsed = perf_counter() - started

    assert result.state == "timeout"
    assert result.error_type == "ShadowRetrievalTimeout"
    assert elapsed < 0.5


def test_exact_and_semantic_variants_are_distinct_memory_providers():
    corpus, _ = build_experiment()
    request = MemoryRequest(project="agent_crew", task_id="326", limit=10)

    exact = shadow_retrieve(ScopeIsolatedProvider(ExactLookupProvider(corpus)), request)
    lexical = shadow_retrieve(ScopeIsolatedProvider(LexicalProvider(corpus)), request)
    hybrid = shadow_retrieve(ScopeIsolatedProvider(HybridProvider(corpus)), request)

    assert exact.provider == "scope_isolated(exact_lookup)"
    assert lexical.provider == "scope_isolated(lexical)"
    assert hybrid.provider == "scope_isolated(hybrid)"
    assert all(item.project == "agent_crew" for item in exact.items + lexical.items + hybrid.items)
