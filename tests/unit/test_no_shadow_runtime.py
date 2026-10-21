"""Owner 2026-10-21: CEA has no operational shadow mode, and ``test`` mode is
test-harness only (review of PR #818).

The in-process tests below run *inside* a pytest session that
``tests/conftest.py`` registered with the engine at ``pytest_configure``, so
``test_harness_active`` is true for them and ``test`` resolves to ``TEST``. The
live-process tests spawn a fresh interpreter — the shape of a real dispatcher —
and prove that the same environment variable yields ``off`` there, with no
enforcement and no embedded authorization authority. That holds even when the
standalone process imports ``pytest`` itself (review of PR #818, HIGH): a module
import is not a harness, only the runner's own session hook is.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_crew.cea import engine as cea_engine
from agent_crew.cea.engine import (
    ENFORCE, OFF, TEST, EngineConfig, register_test_harness, resolve_mode,
    unregister_test_harness)
from agent_crew.cea.engine import test_harness_active as harness_active  # not a test item

SRC = str(Path(__file__).resolve().parents[2] / "src")


def test_unset_mode_is_off():
    assert resolve_mode({}, "agent_crew") == OFF
    assert EngineConfig.from_env({}, project="agent_crew").mode == OFF


def test_legacy_shadow_value_is_forced_off():
    assert resolve_mode({"AGENT_CREW_CEA_MODE": "shadow"}, "agent_crew") == OFF
    assert resolve_mode({"AGENT_CREW_CEA_MODE__AGENT_CREW": "shadow",
                         "AGENT_CREW_CEA_MODE": "enforce"}, "agent_crew") == OFF


def test_explicit_enforce_and_test_still_work_in_a_harness():
    assert harness_active() is True, "tests/conftest.py registered this session"
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
    by the live interpreter — did the runner register a pytest session, and is
    pytest still here? — never by an ``env`` mapping or an environment variable.
    Hide the module for the duration of this test and ``test`` demotes to
    ``off`` even with every marker present and the session still registered."""
    monkeypatch.delitem(sys.modules, "pytest")
    assert harness_active() is False
    env = {"AGENT_CREW_CEA_MODE": "test", "PYTEST_CURRENT_TEST": "fake::test (call)"}
    assert resolve_mode(env, "agent_crew") == OFF
    assert EngineConfig.from_env(env, project="agent_crew").mode == OFF
    assert EngineConfig(mode=TEST).mode == OFF
    assert EngineConfig.from_env(dict(os.environ) | {"AGENT_CREW_CEA_MODE": "test"}).mode == OFF


def test_an_imported_pytest_module_is_not_proof_of_a_harness(monkeypatch):
    """The HIGH finding on PR #818: ``"pytest" in sys.modules`` used to be the
    whole proof. With the runner's registration removed — and pytest still
    imported, and ``PYTEST_CURRENT_TEST`` still set by the real runner — the
    gate must say *no harness*, and ``test`` must be ``off``."""
    monkeypatch.setattr(cea_engine, "_HARNESS_SESSIONS", [])
    assert "pytest" in sys.modules
    assert harness_active() is False
    env = {"AGENT_CREW_CEA_MODE": "test", "PYTEST_CURRENT_TEST": "fake::test (call)"}
    assert resolve_mode(env, "agent_crew") == OFF
    assert EngineConfig.from_env(env, project="agent_crew").mode == OFF
    assert EngineConfig(mode=TEST).mode == OFF
    assert EngineConfig(mode=TEST).enforcing is False
    # enforce is untouched by the gate
    assert resolve_mode({"AGENT_CREW_CEA_MODE": "enforce"}, "agent_crew") == ENFORCE


def test_registration_accepts_only_the_live_pytest_config(monkeypatch, pytestconfig):
    """A caller cannot register an arbitrary object as a "session"; the proof is
    the runner's own ``Config``. Unregistering the real one closes the gate."""
    monkeypatch.setattr(cea_engine, "_HARNESS_SESSIONS", [])
    with pytest.raises(TypeError):
        register_test_harness(object())
    with pytest.raises(TypeError):
        register_test_harness({"pytest": True})
    assert harness_active() is False
    register_test_harness(pytestconfig)
    assert harness_active() is True
    assert resolve_mode({"AGENT_CREW_CEA_MODE": "test"}, "agent_crew") == TEST
    unregister_test_harness(pytestconfig)
    assert harness_active() is False
    unregister_test_harness(object())  # unknown objects are ignored, not an error


def test_the_harness_check_holds_at_collection_time():
    """Suites such as ``test_sev0_cea_permanent_fixtures`` build
    ``EngineConfig(mode="test")`` at module import, before any test item is
    current. The harness fact must not depend on ``PYTEST_CURRENT_TEST``: the
    runner registers the session at ``pytest_configure``, before collection."""
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    assert "pytest" in sys.modules
    assert EngineConfig.from_env(env | {"AGENT_CREW_CEA_MODE": "test"}).mode == TEST


# ── live process: the shape of a real dispatcher ─────────────────────────────

_PROBE = r"""
import json, sys
%(prelude)s
from agent_crew.cea.engine import EngineConfig, TEST, resolve_mode, test_harness_active
import os
env = dict(os.environ)
from_env = EngineConfig.from_env(project="agent_crew")
direct = EngineConfig(mode=TEST)
print(json.dumps({
    "pytest_imported": "pytest" in sys.modules,
    "pytest_current_test": os.environ.get("PYTEST_CURRENT_TEST"),
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


def _live_process(extra_env: dict, *, prelude: str = "",
                  pytest_imported: bool = False) -> dict:
    """Run the probe in a fresh interpreter with only what a deployment would
    carry: no pytest, no inherited harness markers unless ``extra_env`` adds
    them back on purpose. ``prelude`` is Python run before the engine import —
    ``import pytest`` for the standalone-imported-pytest regression. The autouse
    fixture points ``HOME`` at a tmp dir, which hides the user site-packages
    pytest lives in, so when the probe must import pytest its package directory
    is put on ``PYTHONPATH`` after ``src``."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("AGENT_CREW_") and k != "PYTEST_CURRENT_TEST"}
    path = [SRC]
    if pytest_imported:
        path.append(str(Path(pytest.__file__).resolve().parents[1]))
    env["PYTHONPATH"] = os.pathsep.join(path)
    env.update(extra_env)
    out = subprocess.run([sys.executable, "-c", _PROBE % {"prelude": prelude}], env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"probe failed ({out.returncode}):\n{out.stderr}"
    report = json.loads(out.stdout.strip().splitlines()[-1])
    assert report["pytest_imported"] is pytest_imported, report
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



def test_standalone_process_that_imports_pytest_is_not_a_harness():
    """The HIGH finding on PR #818 (review-fix-review-818r0913-r1-r1): a
    standalone ``python3`` that merely ``import pytest`` and exports
    ``AGENT_CREW_CEA_MODE=test`` used to get ``mode=test``, ``enforcing=True``,
    embedded authorization permitted and no endpoint. No pytest session was
    ever registered by a runner in that process, so it must resolve to ``off``:
    nothing computed, nothing enforced, direct construction demoted too."""
    report = _live_process({"AGENT_CREW_CEA_MODE": "test"},
                           prelude="import pytest, _pytest.config",
                           pytest_imported=True)
    assert report["harness"] is False
    assert report["resolved"] == OFF
    assert report["mode"] == OFF and report["direct_mode"] == OFF
    assert report["enforcing"] is False and report["recording"] is False
    assert report["endpoint"] is None


def test_standalone_process_with_pytest_and_every_marker_is_still_off():
    """Importing pytest *and* carrying ``PYTEST_CURRENT_TEST`` *and* a
    per-project ``test`` override is still not a harness. Only the runner's
    own ``pytest_configure`` registration is, and a standalone process has none."""
    report = _live_process({"AGENT_CREW_CEA_MODE__AGENT_CREW": "test",
                            "AGENT_CREW_CEA_MODE": "test",
                            "PYTEST_CURRENT_TEST": "tests/unit/x.py::t (call)"},
                           prelude="import pytest",
                           pytest_imported=True)
    assert report["pytest_current_test"] is not None
    assert report["harness"] is False
    assert report["mode"] == OFF and report["direct_mode"] == OFF
    assert report["enforcing"] is False


def test_standalone_process_cannot_register_a_fake_session():
    """Even a process that imports pytest cannot register something that is not
    the live session ``Config``; the engine refuses and stays off."""
    out = _live_process(
        {"AGENT_CREW_CEA_MODE": "test"},
        prelude=(
            "import pytest\n"
            "from agent_crew.cea.engine import register_test_harness\n"
            "for fake in (object(), {'pytest': True}, pytest):\n"
            "    try:\n"
            "        register_test_harness(fake)\n"
            "    except TypeError:\n"
            "        pass\n"
            "    else:\n"
            "        raise SystemExit('fake session accepted: %r' % (fake,))\n"
        ),
        pytest_imported=True)
    assert out["harness"] is False and out["mode"] == OFF
