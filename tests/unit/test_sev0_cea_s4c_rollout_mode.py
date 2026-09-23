"""Rollout config: the engine mode is decided **per project** (step 4c, §7 REMAINING 4).

Before this, one process-wide `AGENT_CREW_CEA_MODE` decided for everything the
server touched. That is not a rollout control, it is a switch: the whole fleet
crosses the shadow→enforce boundary together, so the first project ready to move
waits on the last — and in practice nobody moves.

⛔The mode never changes the **verdict**. P7 gives the engine no shadow branch;
  the engine decides identically in every mode and the config decides only
  whether a non-PROCEED answer *stops the work*. That is what makes the
  fallback direction here safe to be open rather than closed: a typo in an
  operator's environment must not refuse real work, and `shadow` changes nothing
  while still producing the measurement.

`off` is new and is the rollout escape hatch — no verdict computed, nothing
recorded — so that "turn the engine off for this project" is a config change and
not a deploy. It is never the default.
"""

import pytest

from agent_crew.cea.callsites import enforcing, recording
from agent_crew.cea.engine import (
    EMBEDDED_MODES, ENFORCE, MODES, OFF, SHADOW, TEST, EngineConfig,
    project_mode_env_var, resolve_mode)

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
    env = {PROCESS: "shadow", "AGENT_CREW_CEA_MODE__A": "enforce"}
    assert resolve_mode(env, "a") == ENFORCE
    assert resolve_mode(env, "b") == SHADOW, "an unlisted project keeps the process default"


def test_a_project_may_also_be_held_back():
    """Rollout runs both ways: a project that is not ready stays behind a fleet
    that has moved on. A precedence rule that only lets projects move *forward*
    would make enforce irreversible per project."""
    env = {PROCESS: "enforce", "AGENT_CREW_CEA_MODE__LEGACY": "shadow"}
    assert resolve_mode(env, "legacy") == SHADOW
    assert resolve_mode(env, "ready") == ENFORCE


def test_the_default_is_shadow():
    assert resolve_mode({}, None) == SHADOW
    assert resolve_mode({}, "anything") == SHADOW


@pytest.mark.parametrize("value", ["enfroce", "ENFORCE ", "1", "true", "on"])
def test_an_unrecognised_value_falls_back_to_shadow_not_to_enforce(value):
    """⛔The one case worth naming: `ENFORCE ` with a trailing space is
    *recognised* (stripped and lowered), while `enfroce` is not and must not
    enforce by accident. Neither may ever resolve to something stricter than
    what was written."""
    resolved = resolve_mode({PROCESS: value}, None)
    if value.strip().lower() in MODES:
        assert resolved == value.strip().lower()
    else:
        assert resolved == SHADOW


def test_an_empty_value_is_not_a_choice():
    """An exported-but-empty variable falls through to the next source rather
    than being read as a mode; otherwise `export VAR=` would silently pin shadow
    over a process-wide enforce."""
    assert resolve_mode({PROCESS: "enforce", "AGENT_CREW_CEA_MODE__A": ""}, "a") == ENFORCE


# ── what the modes mean at a gate ───────────────────────────────────────────


@pytest.mark.parametrize("mode,stops_work,computes", [
    (OFF, False, False),
    (SHADOW, False, True),
    (TEST, True, True),
    (ENFORCE, True, True),
])
def test_each_mode_says_what_it_does(mode, stops_work, computes):
    config = EngineConfig(mode=mode)
    assert config.enforcing is stops_work
    assert config.recording is computes
    assert enforcing(config) is stops_work
    assert recording(config) is computes


def test_off_is_embedded_and_enforce_is_not():
    """`off` withholds nothing, so there is nothing for a credential boundary to
    protect. `enforce` is the one mode whose verdict withholds work."""
    assert OFF in EMBEDDED_MODES and ENFORCE not in EMBEDDED_MODES


def test_the_config_records_which_project_resolved_it():
    config = EngineConfig.from_env({"AGENT_CREW_CEA_MODE__A": "enforce"}, "a")
    assert (config.mode, config.project) == (ENFORCE, "a")


def test_an_explicit_config_is_not_re_resolved(monkeypatch):
    """A caller holding a config already answered the question. Re-resolving it
    from the environment would make their override advisory."""
    monkeypatch.setenv("AGENT_CREW_CEA_MODE__A", "enforce")
    assert enforcing(EngineConfig(mode=SHADOW), project="a") is False
    assert enforcing(project="a") is True


# ── the queue reads it per project ──────────────────────────────────────────


def test_the_queue_resolves_and_caches_per_project(tmp_db, monkeypatch):
    from agent_crew.queue import TaskQueue

    monkeypatch.setenv(PROCESS, "shadow")
    monkeypatch.setenv("AGENT_CREW_CEA_MODE__HOT", "enforce")
    q = TaskQueue(tmp_db)
    assert q.cea_config("hot").mode == ENFORCE
    assert q.cea_config("cold").mode == SHADOW
    assert q.cea_config().mode == SHADOW
    assert q.cea_config("hot") is q.cea_config("hot"), "resolved once per project"


def test_a_pinned_config_still_wins_for_every_project(tmp_db, monkeypatch):
    from agent_crew.queue import TaskQueue

    monkeypatch.setenv("AGENT_CREW_CEA_MODE__HOT", "enforce")
    q = TaskQueue(tmp_db, cea_config=EngineConfig(mode=SHADOW))
    assert q.cea_config("hot").mode == SHADOW
