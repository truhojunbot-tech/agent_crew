"""#772 rollout: inspect every project state before a guarded tester migration."""

import json

import pytest

from agent_crew.role_mapping import effective_role_mapping
from scripts import migrate_script_tester


@pytest.fixture
def crew_home(tmp_path):
    cases = {
        "explicit_and_roles": {"role_agents": {"implementer": "codex", "reviewer": "claude",
                                               "tester": "gemini"},
                               "roles": [{"role": "tester", "agent": "gemini",
                                          "worktree": "/wt/gemini"}], "port": 8101},
        "explicit_only": {"role_agents": {"implementer": "codex", "reviewer": "claude",
                                          "tester": "claude"}},
        "legacy_roles": {"roles": [{"role": "implementer", "agent": "codex"},
                                    {"role": "reviewer", "agent": "claude"},
                                    {"role": "tester", "agent": "gemini"}]},
        "default": {"port": 8104},
        "already_script": {"role_agents": {"implementer": "codex", "reviewer": "claude",
                                           "tester": "script"}},
        "provider_opt_in": {"role_agents": {"implementer": "codex", "reviewer": "claude",
                                            "tester": "gemini"},
                            "tester_provider_opt_in": True},
    }
    for project, state in cases.items():
        path = tmp_path / project / "state.json"
        path.parent.mkdir()
        path.write_text(json.dumps(state))
    return tmp_path, cases


def test_dry_run_lists_every_state_shape_without_writing(crew_home, capsys):
    home, cases = crew_home
    before = {name: (home / name / "state.json").read_bytes() for name in cases}
    assert migrate_script_tester.main(["--crew-home", str(home)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert {row["project"] for row in report["projects"]} == set(cases)
    changes = {row["project"]: (row["old_tester"], row["new_tester"])
               for row in report["projects"]}
    assert changes == {"explicit_and_roles": ("gemini", "script"),
                       "explicit_only": ("claude", "script"),
                       "legacy_roles": ("gemini", "script"),
                       "default": ("script", "script"),
                       "already_script": ("script", "script"),
                       "provider_opt_in": ("gemini", "gemini")}
    assert report["sha256"] and len(report["sha256"]) == 64
    assert {name: (home / name / "state.json").read_bytes() for name in cases} == before


def test_apply_requires_matching_dry_run_hash_and_preserves_other_state(crew_home):
    home, cases = crew_home
    plan = migrate_script_tester.plan_migration(home)
    with pytest.raises(ValueError, match="report hash"):
        migrate_script_tester.apply_migration(home, "bad-hash")
    assert migrate_script_tester.apply_migration(home, plan["sha256"]) == 3
    for name, original in cases.items():
        state = json.loads((home / name / "state.json").read_text())
        mapping, _ = effective_role_mapping(state, project=name)
        wanted = "gemini" if name == "provider_opt_in" else "script"
        assert mapping["tester"] == wanted
        assert state.get("roles") == original.get("roles")
        assert state.get("port") == original.get("port")
        assert "test_timeout_s" not in state
        assert "test_command" not in state
    with pytest.raises(ValueError, match="report hash"):
        migrate_script_tester.apply_migration(home, plan["sha256"])
    assert migrate_script_tester.apply_migration(
        home, migrate_script_tester.plan_migration(home)["sha256"]) == 0


def test_opt_in_requires_boolean_and_is_inventoried_before_any_write(crew_home):
    home, _ = crew_home
    invalid = home / "provider_opt_in" / "state.json"
    state = json.loads(invalid.read_text())
    state["tester_provider_opt_in"] = 1
    invalid.write_text(json.dumps(state))
    before = {path: path.read_bytes() for path in home.glob("*/state.json")}
    with pytest.raises(ValueError, match="tester_provider_opt_in must be boolean"):
        migrate_script_tester.plan_migration(home)
    assert {path: path.read_bytes() for path in before} == before


def test_optional_suite_config_is_reported_and_hash_guarded(crew_home):
    home, cases = crew_home
    command = "python3 -m pytest -q"
    plain = migrate_script_tester.plan_migration(home)
    assert all("test_timeout_s" not in json.loads(
        (home / name / "state.json").read_text()) for name in cases)
    report = migrate_script_tester.plan_migration(
        home, test_timeout_s=2700, test_command=command)
    for row in report["projects"]:
        assert row["old_test_timeout_s"] is None
        assert row["new_test_timeout_s"] == 2700
        assert row["new_test_command"] == command
    with pytest.raises(ValueError, match="report hash"):
        migrate_script_tester.apply_migration(home, report["sha256"])
    assert migrate_script_tester.apply_migration(
        home, report["sha256"], test_timeout_s=2700, test_command=command) == len(cases)
    for name in cases:
        state = json.loads((home / name / "state.json").read_text())
        assert state["test_timeout_s"] == 2700
        assert state["test_command"] == command
    assert plain["sha256"] != report["sha256"]


def test_timeout_option_does_not_write_command(crew_home):
    home, cases = crew_home
    report = migrate_script_tester.plan_migration(home, test_timeout_s=2700)
    assert migrate_script_tester.apply_migration(
        home, report["sha256"], test_timeout_s=2700) == len(cases)
    for name in cases:
        state = json.loads((home / name / "state.json").read_text())
        assert state["test_timeout_s"] == 2700
        assert "test_command" not in state
