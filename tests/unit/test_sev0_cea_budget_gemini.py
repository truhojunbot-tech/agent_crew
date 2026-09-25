"""Qouta Gemini bucket observations feed CEA's provider budget."""
import json

import pytest

from agent_crew.cea.input_providers.budget import QuotaBudgetProvider
from agent_crew.cea.receipt import BudgetClass, ProviderBudgetState


NOW = 2_000_000_000.0


def budget(tmp_path, cache, *, credit="paid", provider="gemini"):
    directory = tmp_path / "quota"
    monitor = directory / f"{provider}_monitor"
    monitor.mkdir(parents=True, exist_ok=True)
    (monitor / "quota_cache.json").write_text(json.dumps(cache), encoding="utf-8")
    reader = QuotaBudgetProvider(str(directory), credit_class={provider: credit},
                                 clock=lambda: NOW, env={})
    return reader.budget(provider)


def gemini_cache(*fractions):
    return {"project_id": "project-1", "user_tier": 1, "user_tier_name": "free",
            "buckets": [{"modelId": f"model-{i}", "tokenType": "input",
                         "remainingFraction": fraction, "resetTime": "2026-09-25T12:00:00Z"}
                        for i, fraction in enumerate(fractions)],
            "fetched_at": NOW}


@pytest.mark.parametrize("fractions, expected", [
    ((0.0,), BudgetClass.EXHAUSTED),
    ((0.5, 0.05), BudgetClass.CONSTRAINED),
    ((0.8, 0.4), BudgetClass.OK),
])
def test_gemini_uses_most_depleted_valid_bucket(tmp_path, fractions, expected):
    observation = budget(tmp_path, gemini_cache(*fractions))
    assert observation.state is expected
    assert observation.observed_at == NOW


@pytest.mark.parametrize("cache", [
    {"fetched_at": NOW},
    gemini_cache(),
    gemini_cache(None, -0.1, 1.1, True, "0", float("nan"), float("inf")),
    {"fetched_at": NOW, "buckets": {"remainingFraction": 0}},
])
@pytest.mark.parametrize("credit, expected", [
    ("paid", ProviderBudgetState.UNVERIFIED),
    ("plan", BudgetClass.CONSTRAINED),
])
def test_no_parseable_observation_uses_stale_path(tmp_path, cache, credit, expected):
    observation = budget(tmp_path, cache, credit=credit)
    assert observation.state is expected
    assert observation.observed_at == NOW


def test_invalid_buckets_do_not_hide_a_valid_one(tmp_path):
    observation = budget(tmp_path, gemini_cache("0", None, 0.0))
    assert observation.state is BudgetClass.EXHAUSTED


def test_claude_window_cache_still_uses_higher_utilization(tmp_path):
    cache = {"fetched_at": NOW, "five_hour": {"utilization": 0.2},
             "seven_day": {"utilization": 0.95}}
    assert budget(tmp_path, cache, provider="claude").state is BudgetClass.CONSTRAINED
