"""Dispatch hard caps bound total runtime; the idle timer handles silence."""
import pytest

from agent_crew.server import _dispatch_timeout_for_role


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("AGENT_CREW_DISPATCH_TIMEOUT", raising=False)
    monkeypatch.delenv("AGENT_CREW_DISPATCH_TIMEOUT_IMPLEMENTER", raising=False)
    monkeypatch.delenv("AGENT_CREW_DISPATCH_TIMEOUT_REVIEWER", raising=False)


def test_implementer_default_hard_cap_is_one_hour():
    assert _dispatch_timeout_for_role("implementer") == 3600.0


def test_reviewer_default_hard_cap_is_one_hour():
    assert _dispatch_timeout_for_role("reviewer") == 3600.0


def test_tester_default_unchanged_at_900():
    assert _dispatch_timeout_for_role("tester") == 900.0


def test_generic_override_raises_every_role(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DISPATCH_TIMEOUT", "1200")
    assert _dispatch_timeout_for_role("implementer") == 1200.0
    assert _dispatch_timeout_for_role("reviewer") == 1200.0
    assert _dispatch_timeout_for_role("tester") == 1200.0


def test_implementer_specific_override_wins_over_generic(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DISPATCH_TIMEOUT", "1200")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_TIMEOUT_IMPLEMENTER", "2400")
    assert _dispatch_timeout_for_role("implementer") == 2400.0
    # generic still governs every other role
    assert _dispatch_timeout_for_role("reviewer") == 1200.0


def test_implementer_specific_override_alone_does_not_affect_other_roles(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DISPATCH_TIMEOUT_IMPLEMENTER", "2400")
    assert _dispatch_timeout_for_role("implementer") == 2400.0
    assert _dispatch_timeout_for_role("reviewer") == 3600.0


def test_reviewer_specific_override_wins_over_generic(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DISPATCH_TIMEOUT", "1200")
    monkeypatch.setenv("AGENT_CREW_DISPATCH_TIMEOUT_REVIEWER", "2400")
    assert _dispatch_timeout_for_role("reviewer") == 2400.0
    assert _dispatch_timeout_for_role("implementer") == 1200.0


def test_task_context_override_sets_reviewer_timeout():
    assert _dispatch_timeout_for_role("reviewer", {"dispatch_timeout_s": 1500}) == 1500.0


def test_task_context_override_is_clamped_to_one_hour():
    assert _dispatch_timeout_for_role("tester", {"dispatch_timeout_s": 7200}) == 3600.0


@pytest.mark.parametrize("value", [0, -1, "invalid", None, {}, True, float("nan"), float("inf")])
def test_invalid_task_context_override_uses_role_default(value):
    assert _dispatch_timeout_for_role("reviewer", {"dispatch_timeout_s": value}) == 3600.0


def test_missing_task_context_override_preserves_implementer_env_default(monkeypatch):
    monkeypatch.setenv("AGENT_CREW_DISPATCH_TIMEOUT_IMPLEMENTER", "2400")
    assert _dispatch_timeout_for_role("implementer", {}) == 2400.0
