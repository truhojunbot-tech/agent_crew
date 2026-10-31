"""Regression coverage for explicit durable project role mappings (#337)."""

import json
import pytest

from click.testing import CliRunner

from agent_crew.cli import crew
from agent_crew.protocol import TaskRequest
from agent_crew.role_mapping import RoleResolutionError, effective_role_mapping
from agent_crew.server import _load_role_to_agent_with_source, _load_worktree_map, create_app


def _write_state(tmp_path, project, state):
    project_dir = tmp_path / project
    project_dir.mkdir()
    path = project_dir / "state.json"
    path.write_text(json.dumps(state))
    return path


def test_roles_set_persists_unique_mapping_and_dispatches_in_its_provider_worktree(tmp_path):
    state_path = _write_state(tmp_path, "demo", {"roles": [
        {"role": "implementer", "agent": "claude", "worktree": "/wt/claude"},
        {"role": "reviewer", "agent": "codex", "worktree": "/wt/codex"},
        {"role": "tester", "agent": "gemini", "worktree": "/wt/gemini"},
    ]})
    result = CliRunner().invoke(crew, [
        "roles", "set", "demo",
        "implementer=codex", "reviewer=claude", "tester=script",
        "--base", str(tmp_path),
    ])

    assert result.exit_code == 0, result.output
    persisted = json.loads(state_path.read_text())
    assert persisted["role_agents"] == {
        "implementer": "codex", "reviewer": "claude", "tester": "script",
    }
    assert "Restart the server" in result.output
    assert _load_role_to_agent_with_source(str(state_path)) == (
        persisted["role_agents"], "explicit project config",
    )
    assert _load_worktree_map(str(state_path)) == {
        "implementer": "/wt/codex", "reviewer": "/wt/claude",
    }
    app = create_app(db_path=str(tmp_path / "tasks.db"), state_path=str(state_path),
                     watchdog_disabled=True, anomaly_disabled=True)
    task = TaskRequest(task_id="impl-337", task_type="implement", description="x", project="demo")
    assert app.state.resolve_dispatch_target(task, "implementer") == ("codex", "/wt/codex")


def test_explicit_agent_without_worktree_is_omitted_not_assigned_another_provider_checkout(tmp_path):
    state_path = _write_state(tmp_path, "demo", {
        "roles": [
            {"role": "implementer", "agent": "claude", "worktree": "/wt/claude"},
            {"role": "reviewer", "agent": "codex", "worktree": "/wt/codex"},
            {"role": "tester", "agent": "gemini", "worktree": "/wt/gemini"},
        ],
        "role_agents": {"implementer": "codx", "reviewer": "claude", "tester": "script"},
    })

    assert _load_worktree_map(str(state_path)) == {}
    with pytest.raises(RoleResolutionError, match="invalid_role_agents"):
        _load_role_to_agent_with_source(str(state_path))


def test_malformed_explicit_mapping_refuses_legacy_resolution():
    state = {"roles": [{"role": "reviewer", "agent": "claude"}],
             "role_agents": {"implementer": "codex"}}

    with pytest.raises(RoleResolutionError, match="invalid_role_agents"):
        effective_role_mapping(state, project="demo")


def test_roles_show_labels_all_sources_and_set_requires_an_existing_project(tmp_path):
    _write_state(tmp_path, "explicit", {"role_agents": {
        "implementer": "codex", "reviewer": "claude", "tester": "script"}})
    _write_state(tmp_path, "legacy", {"roles": [
        {"role": "reviewer", "agent": "claude", "worktree": "/wt/claude"}]})
    _write_state(tmp_path, "default", {})
    runner = CliRunner()

    explicit = runner.invoke(crew, ["roles", "show", "explicit", "--base", str(tmp_path)])
    legacy = runner.invoke(crew, ["roles", "show", "legacy", "--base", str(tmp_path)])
    default = runner.invoke(crew, ["roles", "show", "default", "--base", str(tmp_path)])
    missing = runner.invoke(crew, ["roles", "set", "missing", "implementer=codex",
                                    "reviewer=claude", "tester=gemini", "--base", str(tmp_path)])

    assert "Source: explicit project config" in explicit.output
    assert "Source: legacy state.json.roles" in legacy.output
    assert "Source: hardcoded default" in default.output
    assert missing.exit_code != 0
    assert "not found" in missing.output
