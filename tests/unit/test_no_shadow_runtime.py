"""Owner 2026-10-21: CEA has no operational shadow mode, and ``test`` mode is
test-harness only (review of PR #818).

The in-process tests below run *inside* pytest, so ``test_harness_active`` is
true for them and ``test`` resolves to ``TEST``. The live-process tests spawn a
fresh interpreter with no pytest imported — the shape of a real dispatcher — and
prove that the same environment variable yields ``off`` there, with no
enforcement and no embedded authorization authority.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from agent_crew.cea.engine import (
    ENFORCE, OFF, TEST, EngineConfig, resolve_mode, test_harness_active)

SRC = str(Path(__file__).resolve().parents[2] / "src")


def test_unset_mode_is_off():
    assert resolve_mode({}, "agent_crew") == OFF
    assert EngineConfig.from_env({}, project="agent_crew").mode == OFF


def test_legacy_shadow_value_is_forced_off():
    assert resolve_mode({"AGENT_CREW_CEA_MODE": "shadow"}, "agent_crew") == OFF
    assert resolve_mode({"AGENT_CREW_CEA_MODE__AGENT_CREW": "shadow",
                         "AGENT_CREW_CEA_MODE": "enforce"}, "agent_crew") == OFF


def test_explicit_enforce_and_test_still_work_in_a_harness():
    assert test_harness_active() is True, "pytest is imported in this interpreter"
    assert resolve_mode({"AGENT_CREW_CEA_MODE": "enforce"}, "agent_crew") == ENFORCE
    assert resolve_mode({"AGENT_CREW_CEA_MODE": "test"}, "agent_crew") == TEST
    assert EngineConfig(mode=TEST).mode == TEST


def test_unknown_value_fails_safe_to_off():
    assert resolve_mode({"AGENT_CREW_CEA_MODE": "shdaow"}, "agent_crew") == OFF


def test_direct_shadow_config_is_forced_off():
    assert EngineConfig(mode="shadow").mode == OFF
    assert EngineConfig().mode == OFF


def test_the_harness_check_reads_the_interpreter_not_the_env(monkeypatch):
    """Configuration cannot declare itself a harness. The question is answered
    by the live interpreter — is pytest imported? — never by an ``env`` mapping
    or an environment variable. Hide the module for the duration of this test
    and ``test`` demotes to ``off`` even with every marker present."""
    monkeypatch.delitem(sys.modules, "pytest")
    assert test_harness_active() is False
    env = {"AGENT_CREW_CEA_MODE": "test", "PYTEST_CURRENT_TEST": "fake::test (call)"}
    assert resolve_mode(env, "agent_crew") == OFF
    assert EngineConfig.from_env(env, project="agent_crew").mode == OFF
    assert EngineConfig(mode=TEST).mode == OFF
    assert EngineConfig.from_env(dict(os.environ) | {"AGENT_CREW_CEA_MODE": "test"}).mode == OFF


def test_the_harness_check_holds_at_collection_time():
    """Suites such as ``test_sev0_cea_permanent_fixtures`` build
    ``EngineConfig(mode="test")`` at module import, before any test item is
    current. The harness fact must not depend on ``PYTEST_CURRENT_TEST``."""
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    assert "pytest" in sys.modules
    assert EngineConfig.from_env(env | {"AGENT_CREW_CEA_MODE": "test"}).mode == TEST


# ── live process: the shape of a real dispatcher ─────────────────────────────

_PROBE = r"""
import json, sys
from agent_crew.cea.engine import EngineConfig, TEST, resolve_mode, test_harness_active
import os
env = dict(os.environ)
from_env = EngineConfig.from_env(project="agent_crew")
direct = EngineConfig(mode=TEST)
print(json.dumps({
    "pytest_imported": "pytest" in sys.modules,
    "harness": test_harness_active(),
    "resolved": resolve_mode(env, "agent_crew"),
    "mode": from_env.mode,
    "enforcing": from_env.enforcing,
    "recording": from_env.recording,
    "embedded": from_env.embedded_authorization_permitted,
    "endpoint": from_env.endpoint,
    "direct_mode": direct.mode,
}))
"""


def _live_process(extra_env: dict) -> dict:
    """Run the probe in a fresh interpreter with only what a deployment would
    carry: no pytest, no inherited harness markers unless ``extra_env`` adds
    them back on purpose."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("AGENT_CREW_") and k != "PYTEST_CURRENT_TEST"}
    env["PYTHONPATH"] = SRC
    env.update(extra_env)
    out = subprocess.run([sys.executable, "-c", _PROBE], env=env, check=True,
                         capture_output=True, text=True, timeout=60)
    report = json.loads(out.stdout.strip().splitlines()[-1])
    assert report["pytest_imported"] is False, report
    return report


def test_live_process_demotes_test_mode_to_off():
    """The HIGH finding on PR #818: ``AGENT_CREW_CEA_MODE=test`` in an
    operational process used to yield ``enforcing=True`` with embedded
    authorization and no endpoint — enforcement without a credential boundary.
    Outside a verifiable harness it is now ``off``: nothing computed, nothing
    enforced, and direct ``EngineConfig(mode=TEST)`` is demoted the same way."""
    report = _live_process({"AGENT_CREW_CEA_MODE": "test"})
    assert report["harness"] is False
    assert report["resolved"] == OFF
    assert report["mode"] == OFF and report["direct_mode"] == OFF
    assert report["enforcing"] is False and report["recording"] is False
    assert report["endpoint"] is None


def test_live_process_per_project_test_mode_is_off_too():
    report = _live_process({"AGENT_CREW_CEA_MODE__AGENT_CREW": "test",
                            "AGENT_CREW_CEA_MODE": "enforce"})
    assert report["mode"] == OFF and report["enforcing"] is False


def test_live_process_inherited_pytest_marker_is_not_a_harness():
    """A child a test spawns inherits ``PYTEST_CURRENT_TEST``. That is an
    environment variable, not a harness: without pytest imported in the child,
    ``test`` still resolves to ``off``."""
    report = _live_process({"AGENT_CREW_CEA_MODE": "test",
                            "PYTEST_CURRENT_TEST": "tests/unit/x.py::t (call)"})
    assert report["harness"] is False
    assert report["mode"] == OFF and report["direct_mode"] == OFF


def test_live_process_enforce_is_untouched():
    """The gate is on ``test`` only. Explicit enforcement authority survives,
    and in a live process it is not embedded."""
    report = _live_process({"AGENT_CREW_CEA_MODE": "enforce"})
    assert report["mode"] == ENFORCE and report["enforcing"] is True
    assert report["embedded"] is False


def test_live_process_shadow_is_off():
    report = _live_process({"AGENT_CREW_CEA_MODE": "shadow"})
    assert report["mode"] == OFF and report["recording"] is False
