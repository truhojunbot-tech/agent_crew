"""#368: canonical role defaults and visible mapping drift."""

import json

from click.testing import CliRunner

from agent_crew.cli import crew
from agent_crew.role_mapping import DEFAULT_ROLE_TO_AGENT, effective_role_mapping


CANONICAL = {"implementer": "codex", "reviewer": "claude", "tester": "gemini"}


def test_default_effective_mapping_is_the_canonical_provider_policy():
    mapping, source = effective_role_mapping(None)

    assert mapping == CANONICAL
    assert source == "hardcoded default"
    assert DEFAULT_ROLE_TO_AGENT == CANONICAL


def test_explicit_mapping_beats_legacy_and_legacy_beats_default():
    explicit = {"role_agents": CANONICAL, "roles": [
        {"role": "implementer", "agent": "claude"},
    ]}
    legacy = {"roles": [{"role": "implementer", "agent": "claude"}]}

    assert effective_role_mapping(explicit, project="disagree") == (CANONICAL, "explicit project config")
    assert effective_role_mapping(legacy, project="legacy") == (
        {"implementer": "claude", "reviewer": "claude", "tester": "gemini"},
        "legacy state.json.roles",
    )


def test_status_fails_visibly_when_explicit_and_legacy_mappings_disagree(tmp_path):
    project_dir = tmp_path / "disagree"
    project_dir.mkdir()
    (project_dir / "state.json").write_text(json.dumps({
        "project": "disagree", "session": "unused", "port": 1, "agents": [],
        "role_agents": CANONICAL,
        "roles": [{"role": "implementer", "agent": "claude"}],
    }))

    result = CliRunner().invoke(crew, ["status", "disagree", "--base", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "ROLE MAPPING: FAIL" in result.output
    assert "role_agents wins" in result.output
    assert "implementer" in result.output


def test_status_fails_visibly_when_effective_mapping_is_not_canonical(tmp_path):
    project_dir = tmp_path / "legacy-drift"
    project_dir.mkdir()
    (project_dir / "state.json").write_text(json.dumps({
        "project": "legacy-drift", "session": "unused", "port": 1, "agents": [],
        "roles": [{"role": "implementer", "agent": "claude"}],
    }))

    result = CliRunner().invoke(crew, ["status", "legacy-drift", "--base", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "ROLE MAPPING: FAIL" in result.output
    assert "effective mapping deviates from canonical policy" in result.output
