"""#940: swap authority, locking and CLI source are checked without live ports."""
import json
import hashlib
import hmac
import os
import time

import pytest

from scripts import runtime_swap
from tests.unit.test_sev0_cea_s4e_wiring import KEY, _canonical, write_key, write_snapshot


def test_second_launcher_names_lock_holder(tmp_path):
    directory = tmp_path / "demo"
    directory.mkdir()
    with runtime_swap.swap_lock(directory):
        with pytest.raises(RuntimeError, match=str(os.getpid())):
            with runtime_swap.swap_lock(directory):
                pass
    with runtime_swap.swap_lock(directory):
        pass


def test_go_requires_decision_id():
    with pytest.raises(SystemExit):
        runtime_swap.parse_args(["demo", "a" * 40, "go"])
    assert runtime_swap.parse_args([
        "demo", "a" * 40, "go", "--decision-id", "T0-1", "--actor", "hojun"
    ]).decision_id == "T0-1"


def test_partial_roll_keeps_cli_source_and_names_port(tmp_path, monkeypatch, capsys):
    root = tmp_path / ".agent_crew"
    root.mkdir()
    target = tmp_path / "checkout" / "src"
    target.mkdir(parents=True)
    old = tmp_path / "old-src"
    old.mkdir()
    (root / "cli_src").symlink_to(old)
    for project, port in (("alpha", 8105), ("beta", 8106)):
        directory = root / project
        directory.mkdir()
        (directory / "state.json").write_text(json.dumps({"port": port}))
    monkeypatch.setattr(runtime_swap, "api", lambda port, path, project: {
        "build": {"commit": "a" * 40 if port == 8105 else "b" * 40},
        "project": project,
    })
    assert runtime_swap.repoint_cli_source(tmp_path, target, "a" * 40) is False
    assert (root / "cli_src").resolve() == old
    assert "8106" in capsys.readouterr().out
    monkeypatch.setattr(runtime_swap, "api", lambda port, path, project: {
        "build": {"commit": "a" * 40}, "project": project,
    })
    assert runtime_swap.repoint_cli_source(tmp_path, target, "a" * 40) is True
    assert (root / "cli_src").resolve() == target


def test_expired_signed_decision_refuses(tmp_path, monkeypatch):
    sha = "a" * 40
    snapshot = write_snapshot(tmp_path, principals=("owner:hojun",),
                              build_commits=(sha,), runtimes=("agent_crew",))
    key = write_key(tmp_path)
    env = {"AGENT_CREW_CEA_MODE": "shadow",
           "AGENT_CREW_CEA_SNAPSHOT_PATH": str(snapshot),
           "AGENT_CREW_CEA_SNAPSHOT_KEY_FILE": str(key)}
    runtime_swap.verify_swap_decision("agent_crew", sha, "T0-4E-1", env, "hojun")
    with pytest.raises(RuntimeError, match="T0 decision refused"):
        runtime_swap.verify_swap_decision("other_project", sha, "T0-4E-1", env, "hojun")
    with pytest.raises(RuntimeError, match="T0 decision refused"):
        runtime_swap.verify_swap_decision("agent_crew", "b" * 40, "T0-4E-1", env, "hojun")
    doc = json.loads(snapshot.read_text())
    doc["decisions"][0]["expires_at"] = time.time() - 1
    body = {name: value for name, value in doc.items() if name != "signature"}
    doc["signature"]["value"] = hmac.new(KEY, _canonical(body), hashlib.sha256).hexdigest()
    snapshot.write_text(json.dumps(doc))
    with pytest.raises(RuntimeError, match="T0 decision refused"):
        runtime_swap.verify_swap_decision("agent_crew", sha, "T0-4E-1", env, "hojun")


def test_go_rechecks_precheck_and_decision_before_sigterm(tmp_path, monkeypatch):
    from tests.unit.test_runtime_swap import _ready_go

    swap, _, _, _ = _ready_go(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(swap, "recheck_precheck", lambda project: calls.append(("precheck", project)))
    def expired(*_args):
        calls.append(("decision", None))
        raise RuntimeError("expired decision")
    monkeypatch.setattr(swap, "verify_swap_decision", expired)
    monkeypatch.setattr(swap.os, "kill", lambda *_args: calls.append(("sigterm", None)))
    with pytest.raises(RuntimeError, match="expired decision"):
        swap.main(["demo", "a" * 40, "go", "--decision-id", "T0-4E-1"])
    assert calls == [("precheck", "demo"), ("decision", None)]


def test_boundary_precheck_refusal_does_not_echo_output(tmp_path, monkeypatch, capsys):
    command = tmp_path / "precheck"
    command.write_text("#!/bin/sh\nprintf 'private-output\\n'\nexit 9\n")
    command.chmod(0o700)
    monkeypatch.setenv("AGENT_CREW_SWAP_PRECHECK", str(command))
    with pytest.raises(RuntimeError, match="rc=9") as error:
        runtime_swap.recheck_precheck("demo")
    assert "private-output" not in str(error.value) + capsys.readouterr().out
