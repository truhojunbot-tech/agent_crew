"""Rollout config: the engine mode is decided **per project** (step 4c, §7 REMAINING 4).

Before this, one process-wide `AGENT_CREW_CEA_MODE` decided for everything the
server touched. That is not a rollout control, it is a switch: the whole fleet
crosses the off→enforce boundary together, so the first project ready to move
waits on the last — and in practice nobody moves.

⛔The mode never changes the **verdict**. P7 gives the engine no mode branch;
  the engine decides identically in every mode that computes a verdict and the
  config decides only whether one is computed and whether a non-PROCEED answer
  *stops the work*. That is what makes the fallback direction here safe to be
  open rather than closed: a typo in an operator's environment must not refuse
  real work.

Owner directive 2026-10-21 (PR #818): there is **no operational shadow mode**.
`off` is the default and the fallback — no verdict computed, nothing recorded,
nothing stopped. `enforce` is the only operational mode that computes a verdict,
and it always enforces it. `test` is enforcement inside a verifiable pytest
harness (`test_harness_active`) and resolves to `off` in any other process; the
live-process proof of that lives in `test_no_shadow_runtime.py`. The historical
value `shadow` is still spelled by old environments and resolves to `off`.
"""

import pytest

from agent_crew.cea.callsites import enforcing, recording
from agent_crew.cea.engine import (
    EMBEDDED_MODES, ENFORCE, MODES, OFF, SHADOW, TEST, EngineConfig,
    project_mode_env_var, resolve_mode, test_harness_active)

PROCESS = "AGENT_CREW_CEA_MODE"


# ── the variable name ───────────────────────────────────────────────────────


@pytest.mark.parametrize("project,expected", [
    ("agent_crew", "AGENT_CREW_CEA_MODE__AGENT_CREW"),
    ("agent-crew", "AGENT_CREW_CEA_MODE__AGENT_CREW"),
    ("crypto.trade.pilot", "AGENT_CREW_CEA_MODE__CRYPTO_TRADE_PILOT"),
    ("  spaced name  ", "AGENT_CREW_CEA_MODE__SPACED_NAME"),
])
def test_the_variable_name_is_one_rule(project, expected):
    """Writer and reader must agree on the spelling; a second copy of this rule
    would show up as an override that silently does nothing."""
    assert project_mode_env_var(project) == expected


def test_no_project_means_the_process_wide_variable():
    assert project_mode_env_var("") == PROCESS
    assert project_mode_env_var("   ") == PROCESS


# ── precedence ──────────────────────────────────────────────────────────────


def test_the_per_project_value_wins():
    env = {PROCESS: "off", "AGENT_CREW_CEA_MODE__A": "enforce"}
    assert resolve_mode(env, "a") == ENFORCE
    assert resolve_mode(env, "b") == OFF, "an unlisted project keeps the process default"


def test_a_project_may_also_be_held_back():
    """Rollout runs both ways: a project that is not ready stays behind a fleet
    that has moved on. A precedence rule that only lets projects move *forward*
    would make enforce irreversible per project."""
    env = {PROCESS: "enforce", "AGENT_CREW_CEA_MODE__LEGACY": "off"}
    assert resolve_mode(env, "legacy") == OFF
    assert resolve_mode(env, "ready") == ENFORCE


def test_the_default_is_off():
    """Nothing set means nothing computed. Owner 2026-10-21: the default is no
    longer the observation-only `shadow`, which no longer exists."""
    assert resolve_mode({}, None) == OFF
    assert resolve_mode({}, "anything") == OFF


def test_the_historical_shadow_value_resolves_to_off_at_every_level():
    """Old environments still spell `shadow`. It must neither enforce nor
    resurrect observation-only operation — including when a per-project
    override says `shadow` over a process-wide `enforce` (holding a project
    back is still allowed; it is held back to `off`)."""
    assert resolve_mode({PROCESS: "shadow"}, None) == OFF
    assert resolve_mode({PROCESS: "shadow"}, "a") == OFF
    assert resolve_mode({PROCESS: "enforce", "AGENT_CREW_CEA_MODE__A": "shadow"}, "a") == OFF
    assert SHADOW not in MODES and SHADOW not in EMBEDDED_MODES


@pytest.mark.parametrize("value", ["enfroce", "ENFORCE ", "1", "true", "on", "shdaow"])
def test_an_unrecognised_value_falls_back_to_off_not_to_enforce(value):
    """⛔The one case worth naming: `ENFORCE ` with a trailing space is
    *recognised* (stripped and lowered), while `enfroce` is not and must not
    enforce by accident. Neither may ever resolve to something stricter than
    what was written."""
    resolved = resolve_mode({PROCESS: value}, None)
    if value.strip().lower() in MODES:
        assert resolved == value.strip().lower()
    else:
        assert resolved == OFF


def test_an_empty_value_is_not_a_choice():
    """An exported-but-empty variable falls through to the next source rather
    than being read as a mode; otherwise `export VAR=` would silently pin off
    over a process-wide enforce."""
    assert resolve_mode({PROCESS: "enforce", "AGENT_CREW_CEA_MODE__A": ""}, "a") == ENFORCE


# ── what the modes mean at a gate ───────────────────────────────────────────


def test_this_suite_is_a_verifiable_harness():
    """Every `test`-mode assertion below depends on it: pytest is imported in
    this interpreter and has marked a test as current."""
    assert test_harness_active() is True


@pytest.mark.parametrize("mode,stops_work,computes", [
    (OFF, False, False),
    (TEST, True, True),
    (ENFORCE, True, True),
])
def test_each_mode_says_what_it_does(mode, stops_work, computes):
    config = EngineConfig(mode=mode)
    assert config.mode == mode
    assert config.enforcing is stops_work
    assert config.recording is computes
    assert enforcing(config) is stops_work
    assert recording(config) is computes


def test_no_operational_mode_records_without_enforcing():
    """The defining property of the forbidden `shadow` mode was "compute and
    record, never stop". No value an operator can write produces it any more:
    direct construction of it is `off`, and every remaining mode either
    computes nothing or enforces what it computes."""
    assert EngineConfig(mode=SHADOW).mode == OFF
    for mode in MODES:
        config = EngineConfig(mode=mode)
        assert not (config.recording and not config.enforcing), mode


def test_off_is_embedded_and_enforce_is_not():
    """`off` withholds nothing, so there is nothing for a credential boundary to
    protect. `enforce` is the one operational mode whose verdict withholds work."""
    assert OFF in EMBEDDED_MODES and ENFORCE not in EMBEDDED_MODES


def test_the_config_records_which_project_resolved_it():
    config = EngineConfig.from_env({"AGENT_CREW_CEA_MODE__A": "enforce"}, "a")
    assert (config.mode, config.project) == (ENFORCE, "a")


def test_an_explicit_config_is_not_re_resolved(monkeypatch):
    """A caller holding a config already answered the question. Re-resolving it
    from the environment would make their override advisory."""
    monkeypatch.setenv("AGENT_CREW_CEA_MODE__A", "enforce")
    assert enforcing(EngineConfig(mode=OFF), project="a") is False
    assert enforcing(project="a") is True


# ── the queue reads it per project ──────────────────────────────────────────


def test_the_queue_resolves_and_caches_per_project(tmp_db, monkeypatch):
    from agent_crew.queue import TaskQueue

    monkeypatch.setenv(PROCESS, "shadow")      # a legacy environment, still spelled this way
    monkeypatch.setenv("AGENT_CREW_CEA_MODE__HOT", "enforce")
    q = TaskQueue(tmp_db)
    assert q.cea_config("hot").mode == ENFORCE
    assert q.cea_config("cold").mode == OFF
    assert q.cea_config().mode == OFF
    assert q.cea_config("hot") is q.cea_config("hot"), "resolved once per project"


def test_a_pinned_config_still_wins_for_every_project(tmp_db, monkeypatch):
    from agent_crew.queue import TaskQueue

    monkeypatch.setenv("AGENT_CREW_CEA_MODE__HOT", "enforce")
    q = TaskQueue(tmp_db, cea_config=EngineConfig(mode=OFF))
    assert q.cea_config("hot").mode == OFF
