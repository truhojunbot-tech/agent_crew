"""Owner 2026-10-21: CEA has no operational shadow mode."""

from agent_crew.cea.engine import ENFORCE, OFF, TEST, EngineConfig, resolve_mode


def test_unset_mode_is_off():
    assert resolve_mode({}, "agent_crew") == OFF
    assert EngineConfig.from_env({}, project="agent_crew").mode == OFF


def test_legacy_shadow_value_is_forced_off():
    assert resolve_mode({"AGENT_CREW_CEA_MODE": "shadow"}, "agent_crew") == OFF
    assert resolve_mode({"AGENT_CREW_CEA_MODE__AGENT_CREW": "shadow",
                         "AGENT_CREW_CEA_MODE": "enforce"}, "agent_crew") == OFF


def test_explicit_enforce_and_test_still_work():
    assert resolve_mode({"AGENT_CREW_CEA_MODE": "enforce"}, "agent_crew") == ENFORCE
    assert resolve_mode({"AGENT_CREW_CEA_MODE": "test"}, "agent_crew") == TEST


def test_unknown_value_fails_safe_to_off():
    assert resolve_mode({"AGENT_CREW_CEA_MODE": "shdaow"}, "agent_crew") == OFF

def test_direct_shadow_config_is_forced_off():
    assert EngineConfig(mode="shadow").mode == OFF
    assert EngineConfig().mode == OFF
