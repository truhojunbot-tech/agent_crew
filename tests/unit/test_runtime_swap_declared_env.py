"""#940: only declared project files can supply the next server environment."""

import json
import os

import pytest

from scripts import runtime_swap, spawn_from_env


def test_launch_env_uses_invoker_base_and_declared_files_only(tmp_path, monkeypatch):
    fleet = tmp_path / "fleet.env"
    fleet.write_text("AGENT_CREW_CLOUD_ENABLED=1\nCUSTOM=declared-secret\n")
    cea = tmp_path / "cea.env"
    cea.write_text("AGENT_CREW_CEA_MODE=shadow\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USER", "worker")
    monkeypatch.setenv("PATH", "/bin")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.delenv("LOGNAME", raising=False)
    for key in list(os.environ):
        if key.startswith("LC_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("UNDECLARED_SECRET", "must-not-launch")

    env = spawn_from_env.launch_env(fleet, cea, tmp_path / "src")

    assert env == {
        "HOME": str(tmp_path), "USER": "worker", "PATH": "/bin",
        "LANG": "C.UTF-8", "AGENT_CREW_CLOUD_ENABLED": "1",
        "CUSTOM": "declared-secret", "AGENT_CREW_CEA_MODE": "shadow",
        "PYTHONPATH": str(tmp_path / "src"),
    }


def test_launch_env_accepts_missing_optional_base_keys(tmp_path, monkeypatch):
    fleet = tmp_path / "fleet.env"
    fleet.write_text("")
    cea = tmp_path / "cea.env"
    cea.write_text("AGENT_CREW_CEA_MODE=shadow\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/bin")
    for key in ("USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE"):
        monkeypatch.delenv(key, raising=False)
    env = spawn_from_env.launch_env(fleet, cea, tmp_path / "src")
    assert env == {"HOME": str(tmp_path), "PATH": "/bin", "PYTHONPATH": str(tmp_path / "src"),
                   "AGENT_CREW_CEA_MODE": "shadow"}


def test_launch_env_copies_optional_locale_keys_when_present(tmp_path, monkeypatch):
    fleet = tmp_path / "fleet.env"
    fleet.write_text("")
    cea = tmp_path / "cea.env"
    cea.write_text("AGENT_CREW_CEA_MODE=shadow\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/bin")
    monkeypatch.setenv("LOGNAME", "operator")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    env = spawn_from_env.launch_env(fleet, cea, tmp_path / "src")
    assert env["LOGNAME"] == "operator"
    assert env["LC_ALL"] == "C.UTF-8"


def test_missing_fleet_and_foreign_cea_refuse(tmp_path, monkeypatch):
    project = tmp_path / "project-b"
    project.mkdir()
    cea = project / "cea.env"
    cea.write_text("AGENT_CREW_CEA_MODE=shadow\n")
    with pytest.raises(RuntimeError, match="fleet.env missing"):
        runtime_swap.project_env_files(project)

    (project / "fleet.env").write_text("CUSTOM=ok\n")
    foreign = tmp_path / "project-a" / "cea.env"
    foreign.parent.mkdir()
    foreign.write_text("AGENT_CREW_CEA_MODE=shadow\n")
    monkeypatch.setenv("AGENT_CREW_SWAP_CEA_ENV_FILE", str(foreign))
    with pytest.raises(RuntimeError, match="project-local cea.env"):
        runtime_swap.project_env_files(project)
    monkeypatch.delenv("AGENT_CREW_SWAP_CEA_ENV_FILE")
    cea.unlink()
    cea.symlink_to(foreign)
    with pytest.raises(RuntimeError, match="project-local cea.env"):
        runtime_swap.project_env_files(project)


def test_env_diff_refuses_unless_exact_key_names_are_accepted(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    live = b"HOME=/tmp\0ONLY_LIVE=hidden-live-value\0"
    declared = {"HOME": "/tmp", "ONLY_DECLARED": "hidden-declared-value"}
    with pytest.raises(RuntimeError, match="ONLY_LIVE"):
        runtime_swap.check_env_diff(live, declared, evidence, set())
    diff = json.loads((evidence / "env_diff.json").read_text())
    assert diff == {"live_but_undeclared": ["ONLY_LIVE"],
                    "declared_but_not_live": ["ONLY_DECLARED"]}
    with pytest.raises(RuntimeError, match="exactly"):
        runtime_swap.check_env_diff(live, declared, evidence, {"ONLY_LIVE"})
    runtime_swap.check_env_diff(live, declared, evidence,
                                {"ONLY_LIVE", "ONLY_DECLARED"})
    assert "hidden-live-value" not in (evidence / "env_diff.json").read_text()
    assert "hidden-declared-value" not in (evidence / "env_diff.json").read_text()


def test_diff_diagnostics_never_print_env_values(tmp_path, capsys):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    with pytest.raises(RuntimeError) as error:
        runtime_swap.check_env_diff(
            b"SECRET_KEY=private-live-value\0",
            {"DECLARED_KEY": "private-declared-value"}, evidence, set())
    output = str(error.value) + capsys.readouterr().out + capsys.readouterr().err
    assert "private-live-value" not in output
    assert "private-declared-value" not in output
    assert "SECRET_KEY" in output and "DECLARED_KEY" in output


def test_accept_env_diff_argument_names_exact_keys():
    args = runtime_swap.parse_args([
        "demo", "a" * 40, "preflight", "--accept-env-diff", "A,B"])
    assert args.accept_env_diff == {"A", "B"}
