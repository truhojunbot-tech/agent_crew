"""Implementer tasks were hitting the shared 900s dispatch timeout while
still legitimately working (observed live on alpha_engine 2026-08-27: 3
consecutive dispatcher_timeout kills on implement tasks whose own dispatch
log showed active tool calls right up to the kill). _dispatch_timeout_for_role
gives the implementer role a longer default while leaving every other role's
900s unchanged, and stays overridable via env vars either way.
"""
import pytest

from agent_crew.server import _dispatch_timeout_for_role


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("AGENT_CREW_DISPATCH_TIMEOUT", raising=False)
    monkeypatch.delenv("AGENT_CREW_DISPATCH_TIMEOUT_IMPLEMENTER", raising=False)


def test_implementer_default_is_longer_than_900():
    assert _dispatch_timeout_for_role("implementer") == 1800.0


def test_reviewer_default_unchanged_at_900():
    assert _dispatch_timeout_for_role("reviewer") == 900.0


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
    assert _dispatch_timeout_for_role("reviewer") == 900.0
